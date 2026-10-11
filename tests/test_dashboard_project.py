"""The dashboard on a user's project: the live release comes from what production's traces carry."""

import json
import subprocess

from medic import dashboard


def test_a_users_project_shows_the_release_production_runs(tmp_path):
    repo = tmp_path / "app"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "medic.toml").write_text('[agent]\nentrypoint = "app:answer"\n')
    for msg in ("first", "Cap passages at 120 characters"):
        (repo / "f.txt").write_text(msg)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", msg], cwd=repo,
                       check=True)
    live = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    (repo / ".medic").mkdir()
    trace = {"trace_id": "t1", "started_at": "2026-10-12T00:00:00Z", "release": live[:12], "status": "OK",
             "question": "q", "answer": "a"}
    (repo / ".medic" / "state.json").write_text(json.dumps({"traces": [trace]}))
    state = dashboard.build_state(repo / ".medic")
    assert state["deploy"]["sha"] == live
    assert state["deploy"]["subject"] == "Cap passages at 120 characters"


def test_no_traces_yet_means_no_live_release_rather_than_a_guess(tmp_path):
    repo = tmp_path / "app"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "medic.toml").write_text('[agent]\nentrypoint = "app:answer"\n')
    (repo / ".medic").mkdir()
    assert dashboard.build_state(repo / ".medic")["deploy"] is None
