"""`agent-medic doctor`: is this project ready for Medic, and if not, exactly what to fix.

Each check prints ok, warn or fail with one line on how to fix it. Nothing is changed and
nothing is sent anywhere except the read-only calls a check names (neatlogs, GitHub).
"""

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    fix: str = ""


def _run(cmd: list[str], cwd: Path | None = None, timeout: float = 20,
         env: dict | None = None) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None


def project(root: Path) -> tuple[Check, object]:
    from medic import project as project_mod
    found = project_mod.find(root)
    if not found:
        return Check("medic.toml", FAIL, "no medic.toml here or in a parent folder",
                     "run `agent-medic init` in your app's folder, review the diff, then `agent-medic init --write`"), None
    try:
        proj = project_mod.load(found)
    except Exception as exc:  # noqa: BLE001 - a broken config is the finding
        return Check("medic.toml", FAIL, f"{found} can't be read: {exc}", "fix the file or re-run `agent-medic init`"), None
    return Check("medic.toml", OK, f"{found} (entrypoint {proj.entrypoint}, workflow '{proj.workflow}')"), proj


def interpreter(proj) -> Check:
    python = (proj.root / proj.python) if proj.python else None
    if python and not python.exists():
        return Check("app interpreter", FAIL, f"{proj.python} doesn't exist",
                     "point [agent] python in medic.toml at your app's virtualenv python, or remove the line")
    exe = str(python) if python else sys.executable     # what Medic itself uses when medic.toml names none
    module, _, func = proj.entrypoint.partition(":")
    code = (f"import sys; sys.path.insert(0, {str(proj.root)!r}); import medic_runtime; "
            f"import importlib; m = importlib.import_module({module!r}); getattr(m, {func!r})")
    # MEDIC_RUNNER=1, as Medic's own runs set it: the app's neatlogs.init stands down, so the
    # check sends nothing anywhere.
    out = _run([exe, "-c", code], cwd=proj.root, timeout=120, env={**os.environ, "MEDIC_RUNNER": "1"})
    if out is None or out.returncode != 0:
        tail = ((out.stderr or out.stdout).strip().splitlines() or ["(no output)"])[-1] if out else "did not run"
        return Check("app interpreter", FAIL, f"can't import {proj.entrypoint} with {exe}: {tail}",
                     "install your app's requirements and agent-medic into that environment "
                     "(pip install -r requirements.txt)")
    return Check("app interpreter", OK, f"{exe} imports {proj.entrypoint} and medic_runtime")


def known_answers(proj) -> Check:
    cases = proj.root / proj.cases
    if not cases.exists():
        return Check("known-answer checks", FAIL, f"{proj.cases} doesn't exist",
                     "run `agent-medic evals bootstrap --questions questions.txt` (a few real questions, one per "
                     "line), or `agent-medic evals bootstrap` once the app has production traffic")
    n = sum(1 for line in cases.read_text(encoding="utf-8").splitlines() if line.strip())
    if n < 3:
        return Check("known-answer checks", WARN, f"only {n} in {proj.cases}",
                     "add questions and re-run `agent-medic evals bootstrap`: a handful catches far more")
    return Check("known-answer checks", OK, f"{n} in {proj.cases}")


def neatlogs_keys() -> list[Check]:
    out = []
    if not os.environ.get("NEATLOGS_API_KEY"):
        out.append(Check("neatlogs API key", FAIL, "NEATLOGS_API_KEY is not set",
                         "add it to your app's .env (neatlogs: Settings -> API Keys)"))
    else:
        out.append(Check("neatlogs API key", OK, "NEATLOGS_API_KEY is set"))
    missing = [k for k in ("NEATLOGS_TOKEN", "NEATLOGS_PROJECT_ID") if not os.environ.get(k)]
    if missing:
        out.append(Check("neatlogs trace reads", FAIL, f"{', '.join(missing)} not set: Medic can't read traces back",
                         "add them to .env (neatlogs: Settings -> Service Accounts, scope observability:read)"))
    else:
        try:
            from bisect_agent.neatlogs_api import NeatlogsReader
            NeatlogsReader()._get("/traces", limit=1)
            out.append(Check("neatlogs trace reads", OK, "traces readable with NEATLOGS_TOKEN"))
        except Exception as exc:  # noqa: BLE001
            out.append(Check("neatlogs trace reads", FAIL, f"reading traces failed: {type(exc).__name__}: {exc}"[:200],
                             "check NEATLOGS_TOKEN and NEATLOGS_PROJECT_ID belong to the same project"))
    if not os.environ.get("NEATLOGS_USER_EMAIL"):
        out.append(Check("neatlogs card updates", WARN, "NEATLOGS_USER_EMAIL is not set",
                         "set it to your neatlogs account email so Medic can update neatlogs' fix cards"))
    return out


def fixer() -> Check:
    from target_agent import llm
    name = llm.provider_name("fixer")
    keys = {"anthropic": "ANTHROPIC_API_KEY", "ollama_cloud": "OLLAMA_API_KEY", "xai": "XAI_API_KEY",
            "bedrock": "AWS_ACCESS_KEY_ID"}
    if name == "scripted":
        return Check("fixer model", FAIL, "the scripted provider can't write fixes",
                     "set FIXER_PROVIDER=anthropic (or ollama_cloud) and its key")
    key = keys.get(name)
    if key and not os.environ.get(key):
        return Check("fixer model", FAIL, f"provider '{name}' needs {key}", f"add {key} to .env, or set FIXER_PROVIDER")
    chosen = os.environ.get("FIXER_PROVIDER") or os.environ.get("LLM_PROVIDER")
    if name == "ollama" and not chosen:
        where = "a local Ollama model" if shutil.which("ollama") else "a local Ollama, which isn't installed"
        return Check("fixer model", WARN, f"no fixer key found, so Medic falls back to {where}: "
                     "small local models write weak fixes",
                     "set ANTHROPIC_API_KEY (or FIXER_PROVIDER=ollama_cloud with OLLAMA_API_KEY)")
    try:
        model = llm.model_for("fixer")
    except Exception as exc:  # noqa: BLE001
        return Check("fixer model", FAIL, str(exc), "set FIXER_MODEL")
    return Check("fixer model", OK, f"{name} / {model}")


def git_repo(proj) -> list[Check]:
    out = []
    root = proj.root
    inside = _run(["git", "rev-parse", "--is-inside-work-tree"], cwd=root)
    if not inside or inside.stdout.strip() != "true":
        return [Check("git", FAIL, f"{root} is not a git repository", "Medic bisects and fixes commits: `git init`")]
    head = _run(["git", "rev-parse", "--short", "HEAD"], cwd=root)
    out.append(Check("git", OK, f"repository at {head.stdout.strip() if head else '?'}"))
    if proj.mode == "pr":
        from medic import delivery
        try:
            repo = delivery.repo(root)
            out.append(Check("GitHub remote", OK, f"PRs go to {repo}"))
        except Exception as exc:  # noqa: BLE001
            out.append(Check("GitHub remote", FAIL, str(exc), "add your GitHub repo as `origin`"))
        if not shutil.which("gh"):
            out.append(Check("GitHub CLI", FAIL, "gh is not installed", "install it (https://cli.github.com) and `gh auth login`"))
        else:
            auth = _run(["gh", "auth", "status"], cwd=root)
            out.append(Check("GitHub CLI", OK, "gh is logged in") if auth and auth.returncode == 0 else
                       Check("GitHub CLI", FAIL, "gh is not logged in", "run `gh auth login`"))
    return out


def tracing(proj) -> Check:
    """The app has to trace into neatlogs with release and env tags, or Medic sees nothing."""
    text = "\n".join(p.read_text(encoding="utf-8", errors="replace") for f in proj.files
                     if (p := proj.root / f).exists())
    if "neatlogs.init(" not in text:
        return Check("app tracing", WARN, "no neatlogs.init(...) in the files medic.toml lists",
                     "re-run `agent-medic init`: it adds neatlogs tracing tagged with env and release")
    if "release:" not in text:
        return Check("app tracing", WARN, "neatlogs.init has no release tag",
                     'add tags=["env:" + MEDIC_ENV, "release:" + GIT_SHA[:12]] so Medic can tell releases apart')
    return Check("app tracing", OK, "neatlogs.init with env and release tags; deploy with GIT_SHA set")


def run(root: Path) -> list[Check]:
    checks = []
    first, proj = project(root)
    checks.append(first)
    checks += neatlogs_keys()
    checks.append(fixer())
    if proj is not None:
        checks.append(tracing(proj))
        checks.append(known_answers(proj))
        checks += git_repo(proj)
        checks.append(interpreter(proj))
    return checks


def render(checks: list[Check]) -> str:
    mark = {OK: "  ok  ", WARN: " warn ", FAIL: " FAIL "}
    lines = []
    for c in checks:
        lines.append(f"[{mark[c.status]}] {c.name}: {c.detail}")
        if c.fix and c.status != OK:
            lines.append(f"         fix: {c.fix}")
    fails = sum(c.status == FAIL for c in checks)
    warns = sum(c.status == WARN for c in checks)
    lines.append("")
    lines.append("ready: `agent-medic start --dashboard`" if not fails else
                 f"{fails} problem(s) to fix first" + (f", {warns} warning(s)" if warns else ""))
    return "\n".join(lines)
