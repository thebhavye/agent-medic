"""Watcher: reads production traces from neatlogs and notices when behavior drifts.

It keeps a baseline from a healthy window of traffic and compares each recent window to it.
When it freezes the baseline it also snapshots the knowledge-base API's response shapes, so
the dependency investigator can later tell whether the API's contract changed.
"""

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from medic import contract, evidence, paths

RECENT = 6          # traces per window
MAX_NEW = 6         # traces read per poll: one window
HEALTHY_FAILURE_RATE = 0.2


@dataclass
class Signal:
    name: str
    metric: str
    baseline: float
    current: float

    def describe(self) -> str:
        if self.metric.endswith("_rate"):
            return f"{self.metric} {self.baseline:.0%} -> {self.current:.0%}"
        ratio = self.current / self.baseline if self.baseline else float("inf")
        return f"{self.metric} {self.baseline:.4g} -> {self.current:.4g} ({ratio:.1f}x)"


# name, metric, kind, threshold: "abs" = rise in a rate, "ratio" = multiple of baseline
RULES = [
    ("tool_errors", "tool_error_rate", "abs", 0.25),
    ("failures", "failure_rate", "abs", 0.25),
    ("loops", "repeated_call_rate", "abs", 0.5),
    ("tool_calls", "tool_calls_per_run", "ratio", 1.5),
    ("cost", "tokens_per_run", "ratio", 1.5),
]


def compare(baseline: dict, current: dict) -> list[Signal]:
    signals = []
    for name, metric, kind, threshold in RULES:
        b, c = baseline.get(metric, 0), current.get(metric, 0)
        if kind == "abs" and c - b >= threshold:
            signals.append(Signal(name, metric, b, c))
        elif kind == "ratio" and b > 0 and c / b >= threshold:
            signals.append(Signal(name, metric, b, c))
    return signals


def load_state() -> dict:
    if paths.STATE.exists():
        return json.loads(paths.STATE.read_text(encoding="utf-8"))
    return {"traces": [], "baseline": None, "contract": None, "healthy_release": None, "since": None}


def save_state(state: dict) -> None:
    paths.STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = paths.STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    tmp.replace(paths.STATE)


class Watcher:
    def __init__(self, reader, log=print) -> None:
        self.reader = reader
        self.log = log
        self.state = load_state()

    def all_evidence(self) -> list[evidence.TraceEvidence]:
        return [evidence.TraceEvidence(**t) for t in self.state["traces"]]

    def poll(self) -> int:
        """Fetch new finalized production traces. Returns how many were added."""
        since = self.state["since"] or (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        seen = {t["trace_id"] for t in self.state["traces"]} | set(self.state.setdefault("skipped", []))
        candidates = [t for t in self.reader.list_traces(workflowName=paths.WORKFLOW, **{"from": since})
                      if t["traceId"] not in seen and "env:prod" in t["tags"]
                      and t.get("finalizationStatus") == "finalized" and self._ours(t["tags"])]
        # Read only the newest traces: under heavy traffic, older ones are already out of every
        # window Medic compares, and reading them all would stall incident work.
        candidates.sort(key=lambda t: t["startedAt"], reverse=True)
        self.state["skipped"] = (self.state["skipped"] + [t["traceId"] for t in candidates[MAX_NEW:]])[-2000:]
        def read(trace):
            try:
                return evidence.collect(self.reader, trace)
            except Exception as exc:   # read it again on the next poll
                self.log(f"[medic] could not read trace {trace['traceId'][:8]} yet: {type(exc).__name__}")
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            new = [e for e in pool.map(read, candidates[:MAX_NEW]) if e is not None]
        new.sort(key=lambda e: e.started_at)
        self.state["traces"].extend(e.to_dict() for e in new)
        self.state["traces"] = self.state["traces"][-400:]
        if new:
            # Re-read a little overlap next time, in case traces finalize out of order.
            self.state["since"] = (datetime.fromisoformat(new[-1].started_at.replace("Z", "+00:00"))
                                   - timedelta(minutes=10)).isoformat()
        save_state(self.state)
        return len(new)

    def _ours(self, tags: list[str]) -> bool:
        """Only traces from releases in this deployment's history (not an earlier deployment's)."""
        release = next((t.split(":", 1)[1] for t in tags if t.startswith("release:")), "")
        cache = self.__dict__.setdefault("_release_cache", {})
        if release not in cache:
            cache[release] = bool(release) and subprocess.run(
                ["git", "cat-file", "-e", f"{release}^{{commit}}"], cwd=paths.DEPLOY, capture_output=True).returncode == 0
        return cache[release]

    def recent(self, release: str | None = None) -> list[evidence.TraceEvidence]:
        evs = self.all_evidence()
        if release:
            evs = [e for e in evs if e.release == release]
        return evs[-RECENT:]

    def check(self) -> tuple[list[Signal], dict]:
        """Compare the latest window with the baseline. Freezes a baseline from the first
        healthy window, and tracks the most recent release that served healthy traffic."""
        recent = self.recent()
        if len(recent) < RECENT:
            return [], {}
        current = evidence.window(recent)
        if self.state["baseline"] is None:
            if current["failure_rate"] <= HEALTHY_FAILURE_RATE:
                self.state["baseline"] = current
                self.state["contract"] = contract.snapshot(paths.KB_URL, [e.question for e in recent])
                self.state["healthy_release"] = recent[-1].release
                save_state(self.state)
                self.log(f"baseline frozen from {current['n']} healthy traces on release {recent[-1].release[:8]}; "
                         "dependency contract recorded")
            return [], current
        signals = compare(self.state["baseline"], current)
        if not signals and len(current["releases"]) == 1 and self._answers_verified(current["releases"][0]):
            self.state["healthy_release"] = current["releases"][0]
            save_state(self.state)
        return signals, current

    def _answers_verified(self, release: str) -> bool:
        """Clean traces aren't enough to call a release healthy: wrong-but-confident answers
        look clean. Once the canary is in use, a release must also pass a canary run of its own."""
        runs = self.state.get("canary") or []
        if not runs:
            return True            # canary not in use: traces are all we have
        mine = [r for r in runs if r.get("release") == release[:12] and "pass_rate" in r]
        base = self.state.get("canary_baseline") or 0
        return bool(mine) and mine[-1]["pass_rate"] >= base - 0.25
