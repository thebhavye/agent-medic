"""The user's project: medic.toml at the root of their agent's repo.

    [agent]
    entrypoint = "app.chat:answer"          # answers one question: f(question) -> str | {"answer": ...}
    files = ["app/chat.py", "app/retrieve.py"]

    [neatlogs]
    workflow = "docs-chatbot"

    [deploy]
    mode = "pr"                             # pr: fixes arrive as pull requests | managed: Medic deploys
    branch = "main"

    [evals]
    cases = ".medic/evals.jsonl"            # known-answer questions; `agent-medic evals bootstrap` builds them

Without a medic.toml Medic runs its own demo world (target_agent, managed deploys).
"""

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from medic import paths

try:
    import tomllib
except ModuleNotFoundError:     # Python 3.10
    import tomli as tomllib

FILE = "medic.toml"


@dataclass
class Project:
    root: Path
    entrypoint: str
    files: list = field(default_factory=list)
    workflow: str = "agent"
    mode: str = "pr"
    branch: str = "main"
    cases: str = ".medic/evals.jsonl"
    python: str = ""                     # the app's own interpreter (its venv); default: Medic's

    @property
    def runner(self) -> list[str]:
        """How Medic runs this agent on a set of questions (see medic/runner.py)."""
        return ["-m", "medic.runner", "--entry", self.entrypoint]


def find(start: Path | None = None) -> Path | None:
    explicit = os.environ.get("MEDIC_PROJECT")
    if explicit:
        path = Path(explicit)
        return path if path.is_file() else (path / FILE if (path / FILE).is_file() else None)
    here = (start or Path.cwd()).resolve()
    for folder in (here, *here.parents):
        if (folder / FILE).is_file():
            return folder / FILE
    return None


def load(path: Path) -> Project:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    agent, deploy = data.get("agent", {}), data.get("deploy", {})
    if not agent.get("entrypoint"):
        raise ValueError(f"{path}: [agent] entrypoint is required (module:function)")
    return Project(root=path.parent.resolve(), entrypoint=agent["entrypoint"], files=list(agent.get("files", [])),
                   workflow=data.get("neatlogs", {}).get("workflow", "agent"), mode=deploy.get("mode", "pr"),
                   branch=deploy.get("branch", "main"), cases=data.get("evals", {}).get("cases", ".medic/evals.jsonl"),
                   python=agent.get("python", ""))


def activate(project: Project) -> Project:
    """Point Medic at the user's project instead of the demo world."""
    if "MEDIC_DIR" not in os.environ:
        paths.MEDIC_DIR = project.root / ".medic"
        paths.STATE = paths.MEDIC_DIR / "state.json"
        paths.INCIDENTS = paths.MEDIC_DIR / "incidents"
        paths.EVIDENCE = paths.MEDIC_DIR / "evidence"
        paths.TRAFFIC = paths.MEDIC_DIR / "traffic"
    paths.DEPLOY = project.root            # the code Medic reads, bisects and fixes: the user's repo
    paths.AGENT_FILES = project.files
    paths.CASES = project.root / project.cases
    paths.WORKFLOW = project.workflow
    paths.RUNNER = project.runner
    if project.python:
        python = Path(project.python)
        paths.PYTHON = str(python if python.is_absolute() else project.root / python)
        os.environ["MEDIC_PYTHON"] = paths.PYTHON                         # for agent-bisect's evaluator
    os.environ["MEDIC_RUNNER_ARGS"] = json.dumps(project.runner)      # for agent-bisect's evaluator
    os.environ["MEDIC_WORKFLOW"] = project.workflow
    paths.PROJECT = project
    if str(project.root) not in sys.path:
        sys.path.insert(0, str(project.root))
    return project


def current() -> Project | None:
    return getattr(paths, "PROJECT", None)


def write(project: Project) -> Path:
    path = project.root / FILE
    path.write_text(render(project), encoding="utf-8")
    return path


def render(project: Project) -> str:
    files = ", ".join(f'"{f}"' for f in project.files)
    return (f'# Agent Medic: how to run and heal this agent. See `agent-medic init --help`.\n\n'
            f'[agent]\nentrypoint = "{project.entrypoint}"\nfiles = [{files}]\n'
            + (f'python = "{project.python}"\n' if project.python else '') + '\n'
            f'[neatlogs]\nworkflow = "{project.workflow}"\n\n'
            f'[deploy]\nmode = "{project.mode}"\nbranch = "{project.branch}"\n\n'
            f'[evals]\ncases = "{project.cases}"\n')
