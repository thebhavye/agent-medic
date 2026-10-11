"""Run the target agent's eval at a given commit and turn it into metrics.

Each evaluation runs in its own git worktree, as a subprocess, against the eval cases
from the current checkout, so every commit is measured on the same inputs. Results are
cached in the run state by key, so nothing is evaluated twice.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import neatlogs

from bisect_agent import gitops, metrics
from bisect_agent.edits import EditError, apply_edits
from bisect_agent.neatlogs_api import NeatlogsReader, summarize
from bisect_agent.state import STATE_DIR, RunState, save

EVAL_TIMEOUT_S = 900


class SkipCommit(Exception):
    """The commit can't be evaluated (fix overlay doesn't apply, or the eval crashed)."""


class Evaluator:
    def __init__(self, repo: Path, state: RunState, cases: Path, reader: NeatlogsReader) -> None:
        self.repo = repo
        self.state = state
        self.cases = cases.resolve()
        self.reader = reader
        self.work = repo / STATE_DIR

    def evaluate_rev(self, sha: str, overlays: list[dict]) -> metrics.Metrics:
        """Evaluate a commit, first applying the fixes in `overlays` (earlier regressions
        already fixed), so a later regression can be bisected on its own."""
        key = sha[:12] + "".join(f"+fix{o['id']}" for o in overlays)
        if key in self.state.evaluations:
            return _metrics(self.state.evaluations[key])
        path = self.work / "wt" / key
        gitops.worktree_add(path, sha, self.repo)
        try:
            for overlay in overlays:
                try:
                    apply_edits(path, overlay["edits"])
                except EditError as exc:
                    raise SkipCommit(f"fix {overlay['id']} does not apply: {exc}") from exc
            return self.evaluate_dir(path, key, sha=sha)
        finally:
            gitops.worktree_remove(path, self.repo)

    def evaluate_dir(self, path: Path, key: str, sha: str = "") -> metrics.Metrics:
        if key in self.state.evaluations:
            return _metrics(self.state.evaluations[key])
        session = f"bisect-{self.state.run_id}-{key}"
        out = self.work / "results" / f"{key}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        self.state.log(f"evaluating {key}", session=session)

        with neatlogs.trace(f"evaluate:{key}", kind="CHAIN", eval_key=key, eval_session=session):
            # The agent runner: the demo agent's, or a project's (MEDIC_RUNNER_ARGS, set by medic.project).
            runner = json.loads(os.environ.get("MEDIC_RUNNER_ARGS") or '["-m", "target_agent.run_eval"]')
            cmd = [os.environ.get("MEDIC_PYTHON") or sys.executable, *runner, "--cases", str(self.cases),
                   "--out", str(out), "--session", session,
                   "--tag", f"run:{self.state.run_id}", "--tag", f"eval:{key}"]
            env = {**os.environ, "PYTHONPATH": str(path)}
            proc = subprocess.run(cmd, cwd=path, env=env, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=EVAL_TIMEOUT_S)
            if proc.returncode != 0 or not out.exists():
                raise SkipCommit(f"eval crashed at {key}: {proc.stderr.strip()[-2000:]}")
            cases = json.loads(out.read_text(encoding="utf-8"))["cases"]
            m = metrics.from_cases(cases)

            if self.reader.enabled:
                traces = self.reader.session_traces(session, expected=len(cases))
                if len(traces) >= len(cases):
                    summary = summarize(traces)
                    # neatlogs may not price a brand-new model yet; keep the local estimate then.
                    if summary["cost_per_run"] > 0:
                        m.cost_per_run = summary["cost_per_run"]
                    m.tokens_per_run = summary["tokens_per_run"] or m.tokens_per_run
                    m.tool_calls_per_run = summary["tool_calls_per_run"] or m.tool_calls_per_run
                    m.source = "neatlogs"
                else:
                    self.state.log(f"only {len(traces)}/{len(cases)} traces in neatlogs for {key}; "
                                   "using local measurements")

        self.state.evaluations[key] = {**m.to_dict(), "sha": sha, "session": session, "results": str(out)}
        save(self.state, self.repo)
        self.state.log(f"{key}: pass {m.pass_rate:.0%}, errors {m.error_rate:.0%}, "
                       f"${m.cost_per_run:.4f}/run, {m.tool_calls_per_run:.1f} tool calls/run [{m.source}]")
        return m


def _metrics(raw: dict) -> metrics.Metrics:
    fields = metrics.Metrics.__dataclass_fields__
    return metrics.Metrics(**{k: v for k, v in raw.items() if k in fields})


def load_cases(raw_eval: dict) -> list[dict]:
    return json.loads(Path(raw_eval["results"]).read_text(encoding="utf-8"))["cases"]
