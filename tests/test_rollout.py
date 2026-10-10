import subprocess
from types import SimpleNamespace

import pytest

from medic import paths, rollout
from medic.evidence import TraceEvidence
from medic.watcher import compare


@pytest.fixture
def deploy(tmp_path, monkeypatch):
    repo = tmp_path / "deploy"
    repo.mkdir()
    git = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (repo / "a.txt").write_text("v1")
    git("add", "-A")
    git("commit", "-qm", "v1")
    git("checkout", "-q", "-b", "fix")
    (repo / "a.txt").write_text("v2")
    git("commit", "-qam", "fix")
    git("checkout", "-q", "main")
    monkeypatch.setattr(paths, "MEDIC_DIR", tmp_path)
    monkeypatch.setattr(paths, "DEPLOY", repo)
    yield repo
    rollout.finish()


def ev(i, release, failed):
    return TraceEvidence(trace_id=f"t{i}", started_at="x", release=release, status="ERROR" if failed else "OK",
                         cost_usd=0, tokens=100, tool_calls=3, duration_ms=1, answer="" if failed else "ok")


def test_route_splits_traffic_and_judge_decides(deploy, monkeypatch):
    state = rollout.start("inc1", "fix")
    assert (rollout.root() / "a.txt").read_text() == "v2"
    monkeypatch.setattr(rollout.random, "random", lambda: 0.1)
    assert rollout.route() == (rollout.root(), state["candidate"])
    monkeypatch.setattr(rollout.random, "random", lambda: 0.9)
    assert rollout.route()[0] == paths.DEPLOY

    baseline = {"failure_rate": 0, "tool_error_rate": 0, "repeated_call_rate": 0, "tool_calls_per_run": 3,
                "tokens_per_run": 100}
    traces = []
    watcher = SimpleNamespace(state={"baseline": baseline},
                              recent=lambda release=None: [t for t in traces if t.release == release])
    assert rollout.judge(watcher, compare) is None                     # no canary traffic yet
    traces += [ev(i, state["candidate"][:12], failed=True) for i in range(4)]
    assert rollout.judge(watcher, compare)["verdict"] == "regressed"
    traces[:] = [ev(i, state["candidate"][:12], failed=False) for i in range(4)]
    assert rollout.judge(watcher, compare)["verdict"] == "healthy"
    rollout.finish()
    # Routing stops at once; the worktree stays for a batch that may still be running from it.
    assert rollout.current() is None and rollout.route()[0] == paths.DEPLOY
