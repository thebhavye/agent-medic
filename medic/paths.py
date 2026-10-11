"""Where Medic keeps things. Everything lives under .medic/ in the project root."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MEDIC_DIR = Path(os.environ.get("MEDIC_DIR", ROOT / ".medic"))
DEPLOY = MEDIC_DIR / "deploy"          # the production deployment: a git repo, one commit per release
STATE = MEDIC_DIR / "state.json"       # watcher state: evidence, baseline, dependency contract
INCIDENTS = MEDIC_DIR / "incidents"
EVIDENCE = MEDIC_DIR / "evidence"
TRAFFIC = MEDIC_DIR / "traffic"
CASES = ROOT / "evals" / "cases.jsonl"
# The deployed agent's own files: its repairable anatomy (not the provider plumbing in llm.py).
AGENT_FILES = ["target_agent/agent.py", "target_agent/tools.py"]
KB_URL = os.environ.get("KB_URL", "http://127.0.0.1:8781")
WORKFLOW = "research-agent"
# How Medic runs the agent over a set of questions (python <RUNNER> --cases --out --session --tag).
# The demo world's runner; a user's project (medic.toml) swaps in medic.runner. See medic/project.py.
RUNNER = ["-m", "target_agent.run_eval"]
# The interpreter that runs the agent: the app's own environment (medic.toml [agent] python).
PYTHON = sys.executable
PROJECT = None
