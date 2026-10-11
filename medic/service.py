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
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


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
    if alive(current.get("watch", {}).get("pid", 0)):
        return {**current, "already": True}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    run = {"started": time.time(), "root": os.getcwd()}
    log = _logs() / f"watch-{stamp}.log"
    run["watch"] = {"pid": _spawn(["watch", "--interval", str(interval)], log), "log": str(log)}
    if dashboard:
        dlog = _logs() / f"dashboard-{stamp}.log"
        run["dashboard"] = {"pid": _spawn(["dashboard", "--port", str(dashboard), "--host", host], dlog),
                            "log": str(dlog), "url": f"http://{'127.0.0.1' if host == '0.0.0.0' else host}:{dashboard}"}
    _run_file().parent.mkdir(parents=True, exist_ok=True)
    _run_file().write_text(json.dumps(run, indent=1), encoding="utf-8")
    return run


def stop() -> list[str]:
    """Stop everything `start` started. Returns what was stopped."""
    run, stopped = _read(), []
    for part in ("watch", "dashboard"):
        pid = (run.get(part) or {}).get("pid")
        if pid and alive(pid):
            _kill_tree(pid)
            stopped.append(f"{part} (pid {pid})")
    if _run_file().exists():
        _run_file().unlink()
    return stopped


def status() -> dict:
    run = _read()
    out = {"running": False}
    for part in ("watch", "dashboard"):
        info = run.get(part)
        if info:
            out[part] = {**info, "alive": alive(info.get("pid", 0))}
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
