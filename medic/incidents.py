"""Incidents: one per detected problem, saved after every phase so Medic resumes after a crash.

    open -> triaged -> investigated -> decided -> remedied -> deployed -> resolved
                                          \\-> escalated (no safe automatic fix; a human is needed)
"""

import json
import time
import uuid
from dataclasses import asdict, dataclass, field

from medic import paths

OPEN_PHASES = ("open", "triaged", "investigated", "decided", "remedied", "deployed")


@dataclass
class Incident:
    signals: list                       # [{"name", "metric", "baseline", "current"}]
    release: str                        # release serving traffic when the incident opened
    healthy_release: str                # most recent release that served healthy traffic
    trace_ids: list                     # the window of traces that triggered it
    id: str = field(default_factory=lambda: time.strftime("%m%d-%H%M%S-") + uuid.uuid4().hex[:4])
    opened_at: float = field(default_factory=time.time)
    phase: str = "open"
    triage: dict = field(default_factory=dict)
    findings: dict = field(default_factory=dict)      # investigator name -> finding
    decision: dict = field(default_factory=dict)
    remedy: dict = field(default_factory=dict)
    deploy: dict = field(default_factory=dict)
    monitor: dict = field(default_factory=dict)
    timeline: list = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.phase in OPEN_PHASES

    def note(self, phase: str, message: str, log=print) -> None:
        self.timeline.append({"t": round(time.time(), 1), "phase": phase, "msg": message})
        log(f"[medic] incident {self.id} {phase}: {message}")


def save(inc: Incident) -> None:
    paths.INCIDENTS.mkdir(parents=True, exist_ok=True)
    path = paths.INCIDENTS / f"{inc.id}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(inc), indent=1), encoding="utf-8")
    tmp.replace(path)


def load_all() -> list[Incident]:
    if not paths.INCIDENTS.exists():
        return []
    return sorted((Incident(**json.loads(p.read_text(encoding="utf-8"))) for p in paths.INCIDENTS.glob("*.json")),
                  key=lambda i: i.opened_at)


def open_incidents() -> list[Incident]:
    return [i for i in load_all() if i.is_open]
