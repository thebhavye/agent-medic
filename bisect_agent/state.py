"""Run state, saved to disk after every step so an interrupted run resumes where it stopped.

Everything agent-bisect learns (evaluated commits, verdicts, culprits, fix attempts) lives
here. Evaluations are the expensive part, so a resumed run never re-runs one it already has.
"""

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

STATE_DIR = ".bisect"
STATE_FILE = "state.json"


@dataclass
class Investigation:
    signature: str                  # which metric regressed: errors | quality | cost | tool_calls
    good_value: float
    bad_value: float
    verdicts: dict = field(default_factory=dict)   # sha -> "good" | "bad" | "skip"
    culprit: str | None = None
    culprit_subject: str = ""
    intent: dict = field(default_factory=dict)     # what Entire (or git) says about why the change was made
    attempts: list = field(default_factory=list)   # fix attempts: edits, metrics, accepted?, feedback
    fix_commit: str | None = None
    status: str = "bisecting"       # bisecting | explaining | fixing | fixed | gave_up | ambiguous


@dataclass
class RunState:
    good: str
    bad: str
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    phase: str = "detect"           # detect | investigate | done
    fix_branch: str = ""
    fix_tip: str = ""
    evaluations: dict = field(default_factory=dict)   # eval key -> metrics dict
    investigations: list = field(default_factory=list)
    events: list = field(default_factory=list)
    started_at: float = field(default_factory=time.time)

    @property
    def current(self) -> Investigation | None:
        if self.investigations and self.investigations[-1].status not in ("fixed", "gave_up", "ambiguous"):
            return self.investigations[-1]
        return None

    def log(self, message: str, **data) -> None:
        self.events.append({"t": round(time.time(), 1), "msg": message, **data})
        print(f"[agent-bisect] {message}", flush=True)


def state_path(repo: Path) -> Path:
    return repo / STATE_DIR / STATE_FILE


def save(state: RunState, repo: Path) -> None:
    path = state_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(state), indent=2), encoding="utf-8")
    os.replace(tmp, path)  # atomic, so a crash mid-write never corrupts the state


def load(repo: Path) -> RunState | None:
    path = state_path(repo)
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["investigations"] = [Investigation(**i) for i in raw.get("investigations", [])]
    return RunState(**raw)
