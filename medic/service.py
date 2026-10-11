"""`agent-medic start | stop | status | logs`: Medic as a background service of the project.

    agent-medic start [--dashboard 8790]   # watch (and the dashboard) in the background
    agent-medic status                     # running? since when? what is it doing?
    agent-medic logs [-n 40]               # the end of its log
    agent-medic stop                       # stops everything it started, nothing left behind

Each part runs as one Python process (`python -m medic.cli ...`), detached from the terminal
that started it, logging to .medic/logs/. Its pid goes to .medic/run/medic.json, and `stop`
ends that process with all of its children (eval runs, worktree checks).
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from medic import incidents, paths


STARTUP_CHECK_S = 2       # how long start waits to see that Medic stays up


def _run_file() -> Path:
    return paths.MEDIC_DIR / "run" / "medic.json"


def _logs() -> Path:
    return paths.MEDIC_DIR / "logs"


def _read() -> dict:
    try:
        return json.loads(_run_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def alive(pid: int) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) on Windows terminates the process: ask the kernel instead.
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, int(pid))      # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259                       # STILL_ACTIVE
    # A child that exited stays a zombie until its parent collects it, and os.kill(pid, 0)
    # still succeeds on a zombie: collect it if it's ours, and never count a zombie as running.
    try:
        done, _ = os.waitpid(int(pid), os.WNOHANG)
        if done:
            return False
    except ChildProcessError:
        pass                                 # not our child: someone else collects it
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{int(pid)}/stat", encoding="utf-8") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True                          # no /proc (macOS): os.kill said it exists


def started_at(pid: int) -> str | None:
    """The OS's record of when process `pid` started, or None if it can't be told. A pid is
    recycled once its process ends: this is how Medic knows a pid is still the one it started."""
    if not pid:
        return None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return None
        times = [wintypes.FILETIME() for _ in range(4)]
        ok = kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times))
        kernel32.CloseHandle(handle)
        if not ok:
            return None
        return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)   # creation FILETIME
    try:
        with open(f"/proc/{int(pid)}/stat", encoding="utf-8") as f:
            return f.read().rsplit(")", 1)[1].split()[19]   # start time, in clock ticks since boot
    except (OSError, IndexError):
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))], capture_output=True, text=True)
        return out.stdout.strip() or None if out.returncode == 0 else None   # macOS / BSD


def ours(info: dict) -> bool:
    """The recorded process is alive and is still the one `start` launched (not a recycled pid)."""
    pid = (info or {}).get("pid", 0)
    if not alive(pid):
        return False
    then = info.get("started_at")
    return then is None or then == started_at(pid)


def command(args: list[str]) -> list[str]:
    """One plain Python process per part (no launcher in between), so its pid is the one to stop."""
    return [sys.executable, "-m", "medic.cli", *args]


def _spawn(args: list[str], log: Path) -> int:
    log.parent.mkdir(parents=True, exist_ok=True)
    out = open(log, "a", encoding="utf-8")      # noqa: SIM115 - handed to the child, which keeps it
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1"}
    kwargs = {"stdout": out, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL, "cwd": os.getcwd(),
              "env": env}
    if os.name == "nt":
        kwargs["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
                                   | subprocess.CREATE_NO_WINDOW)
    else:
        kwargs["start_new_session"] = True      # its own process group: stop ends the whole group
    proc = subprocess.Popen(command(args), **kwargs)
    out.close()
    return proc.pid


def _kill_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    for _ in range(50):
        if not alive(pid):
            return
        time.sleep(0.1)
    with_kill = getattr(signal, "SIGKILL", signal.SIGTERM)
    try:
        os.killpg(os.getpgid(pid), with_kill)
    except (ProcessLookupError, PermissionError):
        pass


def start(interval: float = 20, dashboard: int | None = None, host: str = "127.0.0.1") -> dict:
    """Start Medic for this project in the background. Already running: says so, starts nothing."""
    current = _read()
    if ours(current.get("watch")):
        return {**current, "already": True}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    run = {"started": time.time(), "root": os.getcwd()}
    log = _logs() / f"watch-{stamp}.log"
    pid = _spawn(["watch", "--interval", str(interval)], log)
    run["watch"] = {"pid": pid, "started_at": started_at(pid), "log": str(log)}
    if dashboard:
        dlog = _logs() / f"dashboard-{stamp}.log"
        dpid = _spawn(["dashboard", "--port", str(dashboard), "--host", host], dlog)
        run["dashboard"] = {"pid": dpid, "started_at": started_at(dpid), "log": str(dlog),
                            "url": f"http://{'127.0.0.1' if host == '0.0.0.0' else host}:{dashboard}"}
    _run_file().parent.mkdir(parents=True, exist_ok=True)
    _run_file().write_text(json.dumps(run, indent=1), encoding="utf-8")
    # A Medic that can't run (no medic.toml, a missing key, a bad config) exits at once: say so
    # now, with its last words, rather than report a start that didn't happen.
    for _ in range(STARTUP_CHECK_S * 10):
        if not alive(pid):
            run["failed"] = tail(15)
            if ours(run.get("dashboard")):
                _kill_tree(run["dashboard"]["pid"])     # nothing would track it once the record is gone
            _run_file().unlink()
            break
        time.sleep(0.1)
    return run


def stop() -> list[str]:
    """Stop everything `start` started. Returns what was stopped."""
    run, stopped = _read(), []
    for part in ("watch", "dashboard"):
        info = run.get(part) or {}
        if ours(info):                      # never a recycled pid: that would be someone else's program
            _kill_tree(info["pid"])
            stopped.append(f"{part} (pid {info['pid']})")
    if _run_file().exists():
        _run_file().unlink()
    return stopped


def status() -> dict:
    run = _read()
    out = {"running": False}
    for part in ("watch", "dashboard"):
        info = run.get(part)
        if info:
            out[part] = {**info, "alive": ours(info)}
    out["running"] = bool(out.get("watch", {}).get("alive"))
    out["started"] = run.get("started")
    out["open_incidents"] = [{"id": i.id, "phase": i.phase} for i in incidents.open_incidents()]
    return out


def tail(n: int = 40, part: str = "watch") -> list[str]:
    log = (_read().get(part) or {}).get("log")
    if not log:
        logs = sorted(_logs().glob(f"{part}-*.log")) if _logs().exists() else []
        log = str(logs[-1]) if logs else None
    if not log or not Path(log).exists():
        return []
    lines = Path(log).read_text(encoding="utf-8", errors="replace").splitlines()
    return [line for line in lines if "telemetry" not in line.lower()][-n:]
