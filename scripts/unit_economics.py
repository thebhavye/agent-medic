"""Turn what Agent Medic actually spent into a cost-per-incident model.

Reads incidents, fixer token usage and eval result files from MEDIC_DIR and writes
docs/business/measured.csv plus docs/business/unit_economics.md. Every number traces
back to a file, or is listed in the report's assumptions table.
"""

import argparse
import csv
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# USD per million tokens (input, output). Each row names its source or is an assumption.
PRICES = {
    # Ollama Cloud serves these on a free tier today, so we price them at comparable
    # hosted open-weight endpoints (assumption, 100B+ class and 20B class respectively).
    "as_run_fixer": {"model": "gpt-oss:120b class (assumption)", "in_per_m": 0.50, "out_per_m": 1.50},
    "as_run_eval": {"model": "gpt-oss:20b class (assumption)", "in_per_m": 0.10, "out_per_m": 0.30},
    # Task-given rates for the Claude points.
    "haiku": {"model": "Claude Haiku 5.5 (task rate)", "in_per_m": 0.10, "out_per_m": 0.50},
    "opus": {"model": "Claude Opus 5.5 (task rate)", "in_per_m": 4.00, "out_per_m": 20.00},
}

POINTS = {
    "as_run": ("As run", "as_run_fixer", "as_run_eval"),
    "claude_mix": ("Claude mix (Haiku evals, Opus fixer)", "opus", "haiku"),
    "all_opus": ("All Opus", "opus", "opus"),
}

TERMINAL = {"resolved", "escalated", "done"}
EC2_HOURLY = 0.0208  # t3.small us-east-1 on-demand, assumption; 730 h/month


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def median(values: list[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    if not n:
        return 0.0
    mid = n // 2
    return float(ordered[mid]) if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def p90(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return float(ordered[min(len(ordered) - 1, math.ceil(0.9 * len(ordered)) - 1)])


def token_cost_usd(input_tokens: float, output_tokens: float, price: dict) -> float:
    return input_tokens / 1e6 * price["in_per_m"] + output_tokens / 1e6 * price["out_per_m"]


def sum_fixer_usage(attempts: list) -> tuple[int, int]:
    """Total (input, output) tokens over fix attempts' usage records."""
    total_in, total_out = 0, 0
    for attempt in attempts or []:
        usage = (attempt or {}).get("usage") or {}
        total_in += int(usage.get("input_tokens") or 0)
        total_out += int(usage.get("output_tokens") or 0)
    return total_in, total_out


def sum_eval_tokens(result_files: list[Path]) -> tuple[int, int, int, int]:
    """Total (runs, cases, input, output) tokens over eval result files."""
    runs, cases, total_in, total_out = 0, 0, 0, 0
    for path in result_files:
        data = read_json(path)
        if not isinstance(data, dict):
            continue
        runs += 1
        for case in data.get("cases") or []:
            cases += 1
            total_in += int((case or {}).get("input_tokens") or 0)
            total_out += int((case or {}).get("output_tokens") or 0)
    return runs, cases, total_in, total_out


def detect_resolve_seconds(incident: dict) -> tuple[float | None, float | None]:
    """Detect = first non-open timeline step minus opened_at; resolve = last step."""
    opened = incident.get("opened_at")
    timeline = incident.get("timeline") or []
    if not opened or not timeline:
        return None, None
    detect = next((e["t"] - opened for e in timeline if e.get("phase") != "open"), None)
    return detect, timeline[-1]["t"] - opened


def neatlogs_calls(trace_ids: list, traces_by_id: dict) -> int:
    """Per-trace estimate: 1 span list + 1 payload per tool call + 1 agent payload."""
    total = 0
    for trace_id in trace_ids or []:
        trace = traces_by_id.get(trace_id)
        if not trace:
            continue
        total += 1 + int(trace.get("tool_calls") or 0) + 1
    return total


def collect_incidents(medic_dir: Path) -> list[dict]:
    """Terminal incidents from live, every bench run and external cases."""
    rows = []
    for path in sorted((medic_dir / "incidents").glob("*.json")):
        data = read_json(path)
        if isinstance(data, dict) and data.get("phase") in TERMINAL:
            rows.append({"source": "live", "dir": None, "scenario": "", "data": data})
    for path in sorted(medic_dir.glob("bench/*/incidents/*.json")):
        data = read_json(path)
        if isinstance(data, dict) and data.get("phase") in TERMINAL:
            rows.append({"source": "bench", "dir": path.parent.parent,
                         "scenario": path.parent.parent.name.split("-")[0], "data": data})
    for path in sorted((medic_dir / "external").glob("case-*.json")):
        data = read_json(path)
        if isinstance(data, dict) and data.get("phase") in TERMINAL:
            rows.append({"source": "external", "dir": None,
                         "scenario": data.get("target", ""), "data": data})
    return rows


def eval_files_for(medic_dir: Path, row: dict) -> tuple[list[Path], str]:
    """Eval result files attributable to one row, plus a note on attribution."""
    incident_id = (row["data"] or {}).get("id", "")
    if row["source"] == "bench":
        files = sorted((row["dir"] / "deploy" / ".bisect" / "results").glob("*.json"))
        return files, "all result files in the bench run's deploy dir (bisect + verify)"
    if row["source"] == "live":
        files = sorted((medic_dir / "deploy" / ".bisect" / "results").glob(f"*{incident_id}*.json"))
        return files, "shared deploy results whose file name contains the incident id"
    return [], "external cases run their own harness; no .bisect eval files"


def load_traces(state_path: Path) -> dict:
    state = read_json(state_path) or {}
    traces = state.get("traces") or []
    return {t["trace_id"]: t for t in traces if isinstance(t, dict) and t.get("trace_id")}


def measure_row(medic_dir: Path, row: dict, live_traces: dict) -> dict:
    data = row["data"]
    if row["source"] == "external":
        attempts = data.get("attempts") or []
        cause = (data.get("decision") or {}).get("cause", "")
        outcome = data.get("phase", "")
        detect, resolve = None, None
        trace_ids: list = []
        traces_by_id: dict = {}
    else:
        attempts = (data.get("remedy") or {}).get("attempts") or []
        cause = (data.get("decision") or {}).get("cause", "")
        outcome = data.get("phase", "")
        detect, resolve = detect_resolve_seconds(data)
        bench_result = read_json(row["dir"] / "result.json") if row["dir"] else None
        if isinstance(bench_result, dict):
            detect = bench_result.get("detect_s", detect)
            resolve = bench_result.get("resolve_s", resolve)
        trace_ids = data.get("trace_ids") or []
        traces_by_id = load_traces(row["dir"] / "state.json") if row["dir"] else live_traces
    fixer_in, fixer_out = sum_fixer_usage(attempts)
    eval_files, note = eval_files_for(medic_dir, row)
    runs, cases, eval_in, eval_out = sum_eval_tokens(eval_files)
    return {
        "source": row["source"],
        "incident": data.get("id", ""),
        "scenario_or_target": row["scenario"],
        "cause": cause,
        "outcome": outcome,
        "detect_s": detect,
        "resolve_s": resolve,
        "fix_attempts": len(attempts),
        "fixer_input_tokens": fixer_in,
        "fixer_output_tokens": fixer_out,
        "eval_runs": runs,
        "eval_cases": cases,
        "eval_input_tokens": eval_in,
        "eval_output_tokens": eval_out,
        "neatlogs_api_calls_estimate": neatlogs_calls(trace_ids, traces_by_id),
        "eval_note": note,
    }


def unattributed_shared_evals(medic_dir: Path, measured: list[dict]) -> list[Path]:
    """Shared deploy evals no incident row claimed (e.g. baseline runs)."""
    claimed: set[str] = set()
    for row in measured:
        for path in (medic_dir / "deploy" / ".bisect" / "results").glob(f"*{row['incident']}*.json"):
            claimed.add(path.name)
    return [p for p in sorted((medic_dir / "deploy" / ".bisect" / "results").glob("*.json"))
            if p.name not in claimed]


def per_case_tokens(medic_dir: Path) -> tuple[float, float]:
    """Mean (input, output) tokens per eval case across every measured eval file."""
    files = list((medic_dir / "deploy" / ".bisect" / "results").glob("*.json"))
    files += list(medic_dir.glob("bench/*/deploy/.bisect/results/*.json"))
    runs, cases, total_in, total_out = sum_eval_tokens(files)
    if not cases:
        return 0.0, 0.0
    return total_in / cases, total_out / cases


def monthly_canary_cost(case_in: float, case_out: float, eval_price: dict,
                        interval_minutes: float) -> tuple[float, float, float]:
    """(runs/month, input tokens, cost) for an 8-case canary at a fixed interval."""
    runs = 30 * 24 * 60 / interval_minutes
    monthly_in, monthly_out = runs * 8 * case_in, runs * 8 * case_out
    return runs, monthly_in, token_cost_usd(monthly_in, monthly_out, eval_price)


def fmt_usd(value: float) -> str:
    return f"${value:,.4f}" if value < 1 else f"${value:,.2f}"


def write_csv(rows: list[dict], path: Path):
    columns = ["source", "incident", "scenario_or_target", "cause", "outcome", "detect_s",
               "resolve_s", "fix_attempts", "fixer_input_tokens", "fixer_output_tokens",
               "eval_runs", "eval_cases", "eval_input_tokens", "eval_output_tokens",
               "neatlogs_api_calls_estimate"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_report(medic_dir: Path, rows: list[dict], out: Path, interval_minutes: float):
    by_cause: dict[str, list[dict]] = {}
    for row in rows:
        by_cause.setdefault(row["cause"] or "unknown", []).append(row)
    fixer_totals = [r["fixer_input_tokens"] + r["fixer_output_tokens"] for r in rows]
    eval_totals = [r["eval_input_tokens"] + r["eval_output_tokens"] for r in rows]
    detects = [r["detect_s"] for r in rows if r["detect_s"] is not None]
    resolves = [r["resolve_s"] for r in rows if r["resolve_s"] is not None]
    attempts = [r["fix_attempts"] for r in rows]
    med_fixer = median(fixer_totals)
    med_eval = median(eval_totals)
    case_in, case_out = per_case_tokens(medic_dir)
    unattributed = unattributed_shared_evals(medic_dir, rows)

    lines = ["# Agent Medic unit economics", "",
             f"Source: `{medic_dir}` - {len(rows)} terminal incidents "
             f"({sum(1 for r in rows if r['source'] == 'live')} live, "
             f"{sum(1 for r in rows if r['source'] == 'bench')} bench, "
             f"{sum(1 for r in rows if r['source'] == 'external')} external).",
             "Raw rows: `measured.csv`.", ""]
    lines += ["## 1. Measured per incident", "",
              f"- Fixer tokens: median {med_fixer:,.0f}, p90 {p90(fixer_totals):,.0f}.",
              f"- Eval tokens: median {med_eval:,.0f}, p90 {p90(eval_totals):,.0f}.",
              f"- Time to detect: median {median(detects):,.0f}s, p90 {p90(detects):,.0f}s.",
              f"- Time to resolve: median {median(resolves):,.0f}s, p90 {p90(resolves):,.0f}s.",
              f"- Fix attempts: median {median(attempts):.1f}, p90 {p90(attempts):.1f}.", "",
              "| cause | n | median fixer tok | median eval tok | median detect | median resolve |",
              "|---|---|---|---|---|---|"]
    for cause, group in sorted(by_cause.items()):
        lines.append(f"| {cause} | {len(group)} "
                     f"| {median([r['fixer_input_tokens'] + r['fixer_output_tokens'] for r in group]):,.0f} "
                     f"| {median([r['eval_input_tokens'] + r['eval_output_tokens'] for r in group]):,.0f} "
                     f"| {median([r['detect_s'] for r in group if r['detect_s'] is not None]):,.0f}s "
                     f"| {median([r['resolve_s'] for r in group if r['resolve_s'] is not None]):,.0f}s |")
    lines += ["", "## 2. Cost per incident at three price points", "",
              "Median incident: "
              f"{med_fixer:,.0f} fixer tokens, {med_eval:,.0f} eval tokens. "
              "Fixer/output split follows the median row; eval tokens use the "
              "median eval input/output split.", "",
              "| price table (USD per M tokens) | input | output | source |",
              "|---|---|---|---|"]
    for key, price in PRICES.items():
        lines.append(f"| {key}: {price['model']} | {price['in_per_m']} | {price['out_per_m']} "
                     f"| {price['model'].split('(')[-1].rstrip(')')} |")
    lines += ["", "| price point | per incident |", "|---|---|"]
    point_costs = {}
    for key, (label, fixer_key, eval_key) in POINTS.items():
        med_row = min(rows, key=lambda r: abs((r["fixer_input_tokens"] + r["fixer_output_tokens"]) - med_fixer))
        cost = (token_cost_usd(med_row["fixer_input_tokens"], med_row["fixer_output_tokens"],
                               PRICES[fixer_key])
                + token_cost_usd(med_row["eval_input_tokens"], med_row["eval_output_tokens"],
                                 PRICES[eval_key]))
        point_costs[key] = cost
        lines.append(f"| {label} | {fmt_usd(cost)} |")
    lines += ["", "## 3. Monitoring overhead per watched agent per month", "",
              f"Canary every {interval_minutes:g} minutes, 8 cases per run. Measured mean per "
              f"eval case: {case_in:,.0f} input + {case_out:,.0f} output tokens.", "",
              "| price point | canary runs/mo | canary cost/mo |",
              "|---|---|---|"]
    canary_costs = {}
    for key, (label, _fixer_key, eval_key) in POINTS.items():
        runs, _in, cost = monthly_canary_cost(case_in, case_out, PRICES[eval_key], interval_minutes)
        canary_costs[key] = cost
        lines.append(f"| {label} | {runs:,.0f} | {fmt_usd(cost)} |")
    lines += ["",
              "neatlogs reads for monitoring are not in the token math above; the watcher polls "
              "the trace list continuously (assumption: negligible API cost next to model tokens).",
              "", "## 4. Infrastructure", "",
              f"One EC2 t3.small in us-east-1, 24/7: {EC2_HOURLY}/h x 730h = "
              f"${EC2_HOURLY * 730:,.2f}/month (hourly rate is an assumption).",
              "", "## 5. Assumptions", "",
              "| # | assumption | justification |",
              "|---|---|---|",
              "| 1 | Hosted open-model rates for the as-run point | Ollama Cloud is free-tier today; "
              "priced at comparable hosted 100B+/20B-class per-token rates |",
              "| 2 | EC2 t3.small ~$0.0208/h | on-demand list price from memory; verify before signing |",
              "| 3 | Canary covers the 8 eval cases | matches evals/cases.jsonl today |",
              "| 4 | Watcher neatlogs polling cost ~$0 | trace-list reads are cheap next to model tokens |",
              "| 5 | Escalated incidents cost the same to work | they consume diagnosis + attempts before escalating |",
              "| 6 | External case has no .bisect eval files | it runs its own verification harness |",
              "", "## Eval attribution", "",
              "- Bench rows sum every result file in that bench run's `deploy/.bisect/results/` "
              "(bisect + verify runs are all part of resolving it).",
              "- Live rows sum shared `deploy/.bisect/results/` files whose name contains the incident id.",
              "- neatlogs API estimate per incident: traces read x (1 span list + tool-call payloads "
              "+ 1 agent payload), from the incident's `trace_ids` and matching evidence."]
    if unattributed:
        lines.append(f"- Unattributed shared evals (not in any row): {', '.join(p.name for p in unattributed)} "
                     "(baseline runs).")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"as_run": point_costs["as_run"], "claude_mix": point_costs["claude_mix"],
            "all_opus": point_costs["all_opus"],
            "canary_as_run": canary_costs["as_run"],
            "canary_claude_mix": canary_costs["claude_mix"],
            "canary_all_opus": canary_costs["all_opus"],
            "med_fixer": med_fixer, "med_eval": med_eval,
            "med_detect": median(detects), "med_resolve": median(resolves)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="unit_economics")
    parser.add_argument("--medic-dir", type=Path, default=ROOT / ".medic")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "docs" / "business")
    parser.add_argument("--canary-minutes", type=float, default=10)
    args = parser.parse_args(argv)
    live_traces = load_traces(args.medic_dir / "state.json")
    rows = [measure_row(args.medic_dir, row, live_traces)
            for row in collect_incidents(args.medic_dir)]
    rows.sort(key=lambda r: (r["source"], r["incident"]))
    write_csv(rows, args.out_dir / "measured.csv")
    summary = write_report(args.medic_dir, rows, args.out_dir / "unit_economics.md", args.canary_minutes)
    print(f"{len(rows)} incidents -> {args.out_dir / 'measured.csv'}")
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
