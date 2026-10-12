"""The restore fallback: when no written fix passes, undo the culprit's change to the surfaces
first aid pinned, but only if that's all the culprit changed in those files."""

import subprocess
from types import SimpleNamespace

from bisect_agent import gitops
from medic import remedy, tournament

HEALTHY = 'P = """\nAnswer from the context.\n{q}\n"""\n\ndef helper():\n    return 1\n'
STRICT = 'P = """\nRefuse unless stated.\nNever guess.\n{q}\n"""\n\ndef helper():\n    return 1\n'


def _commit(repo, text, msg):
    (repo / "app.py").write_text(text)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", msg], cwd=repo, check=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()


def _world(tmp_path, monkeypatch, bad_text, pass_rate=1.0):
    repo = tmp_path / "app"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    good = _commit(repo, HEALTHY, "healthy")
    bad = _commit(repo, bad_text, "strict prompt")
    medic_dir = tmp_path / "medic"
    monkeypatch.setattr(remedy.paths, "DEPLOY", repo)
    monkeypatch.setattr(remedy.paths, "MEDIC_DIR", medic_dir)
    monkeypatch.setattr(remedy, "_verify_cases", lambda inc: tmp_path / "cases.jsonl")
    metrics = SimpleNamespace(pass_rate=pass_rate, error_rate=0.0, n=6, to_dict=lambda: {"pass_rate": pass_rate})

    class FakeEvaluator:
        def __init__(self, *a, **k):
            pass

        def evaluate_dir(self, root, key):
            FakeEvaluator.seen = (root / "app.py").read_text()
            return metrics
    monkeypatch.setattr(remedy, "Evaluator", FakeEvaluator)
    monkeypatch.setattr(tournament, "targets", lambda inc: [{"kind": "prompt", "name": "P", "file": "app.py",
                                                            "start": 1, "end": 5}])
    root = medic_dir / "worktrees" / "inc1"
    gitops.git("branch", "medic/fix-inc1", bad, cwd=repo)
    gitops.worktree_add(root, bad, repo, branch="medic/fix-inc1")
    inc = SimpleNamespace(id="inc1", release=bad, healthy_release=good, remedy={"attempts": [{}, {}, {}]},
                          decision={"cause": "code_regression", "remedy": "code_fix"},
                          findings={"regression": {"culprit": bad}},
                          first_aid={"base": good, "values": ["P"], "functions": [], "trial": {"pass_rate": 1.0}})
    return inc, root, FakeEvaluator


def test_restores_the_pinned_prompt_and_commits_it(tmp_path, monkeypatch):
    inc, root, ev = _world(tmp_path, monkeypatch, STRICT)
    record = remedy.restore_attempt(inc, reader=None, log=lambda *_: None)
    assert record["accepted"] and record["n"] == 4
    assert ev.seen == HEALTHY and (root / "app.py").read_text() == HEALTHY
    msg = subprocess.run(["git", "log", "-1", "--format=%B"], cwd=root, capture_output=True, text=True).stdout
    assert msg.startswith("Restore P from") and "Agent-Medic-Incident: inc1" in msg


def test_wont_restore_a_file_where_the_culprit_changed_something_else(tmp_path, monkeypatch):
    inc, _, _ = _world(tmp_path, monkeypatch, STRICT + "\n" * 6 + "LIMIT = 9\n")
    assert remedy.restore_attempt(inc, reader=None, log=lambda *_: None) is None


def test_a_restore_that_fails_verification_is_rejected(tmp_path, monkeypatch):
    inc, root, _ = _world(tmp_path, monkeypatch, STRICT, pass_rate=0.5)
    record = remedy.restore_attempt(inc, reader=None, log=lambda *_: None)
    assert not record["accepted"] and "below" in record["feedback"]
    assert (root / "app.py").read_text() == STRICT


def test_no_proven_first_aid_means_no_restore(tmp_path, monkeypatch):
    inc, _, _ = _world(tmp_path, monkeypatch, STRICT)
    inc.first_aid = {}
    assert remedy.restore_attempt(inc, reader=None, log=lambda *_: None) is None
