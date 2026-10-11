"""`agent-medic start / status / stop`: Medic in the background, and nothing left behind."""

import sys
import time

import pytest

from medic import paths, service

PARENT = ("import subprocess, sys, time\n"
          "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
          "open(sys.argv[1], 'w').write(str(child.pid))\n"
          "print('watching', flush=True)\n"
          "time.sleep(120)\n")


@pytest.fixture
def svc(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "MEDIC_DIR", tmp_path / ".medic")
    monkeypatch.setattr(paths, "INCIDENTS", tmp_path / ".medic" / "incidents")
    monkeypatch.chdir(tmp_path)
    child_file = tmp_path / "child.pid"
    monkeypatch.setattr(service, "command", lambda args: [sys.executable, "-c", PARENT, str(child_file)])
    yield child_file
    service.stop()


def wait_for(cond, seconds=20):
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.2)
    return False


def test_start_status_stop_leaves_nothing_behind(svc):
    child_file = svc
    run = service.start()
    pid = run["watch"]["pid"]
    assert wait_for(child_file.exists)                                  # it started its own child
    child = int(child_file.read_text())
    assert service.alive(pid) and service.alive(child)
    assert service.status()["running"]
    assert wait_for(lambda: "watching" in "\n".join(service.tail(5)))  # its output lands in the log
    assert service.start()["already"]                                   # never a second copy
    assert service.stop() == [f"watch (pid {pid})"]
    assert wait_for(lambda: not service.alive(pid) and not service.alive(child))   # the whole tree
    assert not service.status()["running"] and service.stop() == []


def test_alive_never_harms_the_process_it_asks_about(svc):
    run = service.start()
    pid = run["watch"]["pid"]
    for _ in range(5):
        assert service.alive(pid)                                       # asking doesn't kill it
    service.stop()


def test_stop_never_touches_a_recycled_pid(svc, monkeypatch):
    run = service.start()
    pid = run["watch"]["pid"]
    record = service._read()
    record["watch"]["started_at"] = "another process's start time"     # the pid now belongs to someone else
    service._run_file().write_text(__import__("json").dumps(record), encoding="utf-8")
    killed = []
    monkeypatch.setattr(service, "_kill_tree", killed.append)
    assert service.stop() == [] and killed == []                        # left alone
    assert service.alive(pid)
    monkeypatch.undo()
    service._kill_tree(pid)


def test_a_medic_that_dies_at_once_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "MEDIC_DIR", tmp_path / ".medic")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(service, "command", lambda args: [sys.executable, "-c", "print('no medic.toml here'); raise SystemExit(2)"])
    run = service.start()
    assert "no medic.toml here" in "\n".join(run["failed"])
    assert not service.status()["running"]


def test_a_failed_start_leaves_no_dashboard_behind(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "MEDIC_DIR", tmp_path / ".medic")
    monkeypatch.chdir(tmp_path)

    def command(args):
        if args[0] == "dashboard":                                      # the dashboard would run on
            return [sys.executable, "-c", "import time; time.sleep(120)"]
        return [sys.executable, "-c", "raise SystemExit('bad config')"]
    monkeypatch.setattr(service, "command", command)
    run = service.start(dashboard=8799)
    assert run["failed"] is not None
    assert wait_for(lambda: not service.alive(run["dashboard"]["pid"]))  # stopped with it
