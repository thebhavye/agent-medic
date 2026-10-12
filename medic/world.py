"""The demo's production world, operated by a human (or the chaos script), not by Medic.

    deploy init        create the production deployment (a git repo; each commit is a release)
    release <name>     ship a new release: one of the planted changes in scripts/seed_history.py
    chaos <mode>       change the knowledge-base API under the running agent
"""

import shutil
import subprocess
import sys

import httpx

from medic import paths




def _seed_history():
    """The demo's planted releases (scripts/seed_history.py in the agent-medic repo). Imported
    only by the demo commands: an installed Medic watching a user's project has no scripts/."""
    sys.path.insert(0, str(paths.ROOT / "scripts"))
    import seed_history
    return seed_history


BENIGN = {"docs": 0, "timeout": 1, "careers": 3, "docstring": 4, "rename-title": 6}


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=paths.DEPLOY, check=True, capture_output=True,
                          text=True).stdout.strip()


def head() -> str:
    return git("rev-parse", "HEAD")


def init(force: bool = False) -> str:
    if paths.DEPLOY.exists():
        if not force:
            raise RuntimeError(f"{paths.DEPLOY} exists; use --force to recreate it")
        shutil.rmtree(paths.DEPLOY, onerror=lambda f, p, e: (__import__("os").chmod(p, 0o700), f(p)))
    paths.DEPLOY.mkdir(parents=True)
    for name in ("target_agent", "evals"):
        shutil.copytree(paths.ROOT / name, paths.DEPLOY / name, ignore=shutil.ignore_patterns("__pycache__"))
    (paths.DEPLOY / ".gitignore").write_text(".bisect/\n__pycache__/\n")
    git("init", "-q", "-b", "main")
    git("config", "user.email", "release-bot@halcyon.example")
    git("config", "user.name", "Halcyon release bot")
    git("add", "-A")
    git("commit", "-q", "-m", "Release: research agent v1")
    return head()


def releases() -> dict:
    seed_history = _seed_history()
    return {**{k: seed_history.STEPS[i]["message"].splitlines()[0] for k, i in seed_history.SCENARIOS.items()},
            **{k: seed_history.STEPS[i]["message"].splitlines()[0] for k, i in BENIGN.items()}}


def release(name: str) -> str:
    seed_history = _seed_history()
    index = seed_history.SCENARIOS.get(name, BENIGN.get(name))
    if index is None:
        raise KeyError(f"unknown release {name!r}; choose from {sorted(releases())}")
    return seed_history.apply_step(paths.DEPLOY, seed_history.STEPS[index])


def chaos(mode: str) -> dict:
    resp = httpx.post(f"{paths.KB_URL}/admin/chaos", json={"mode": mode}, timeout=10)
    resp.raise_for_status()
    return resp.json()


def kb_status() -> dict:
    return httpx.get(f"{paths.KB_URL}/admin/chaos", timeout=10).json()
