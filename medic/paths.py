"""Where Medic keeps things. Everything lives under .medic/ in the project root."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MEDIC_DIR = Path(os.environ.get("MEDIC_DIR", ROOT / ".medic"))
DEPLOY = MEDIC_DIR / "deploy"          # the production deployment: a git repo, one commit per release
STATE = MEDIC_DIR / "state.json"       # watcher state: evidence, baseline, dependency contract
INCIDENTS = MEDIC_DIR / "incidents"
EVIDENCE = MEDIC_DIR / "evidence"
TRAFFIC = MEDIC_DIR / "traffic"
CASES = ROOT / "evals" / "cases.jsonl"
KB_URL = os.environ.get("KB_URL", "http://127.0.0.1:8781")
WORKFLOW = "research-agent"
