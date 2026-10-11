"""A user's project: medic.toml, the generic runner, PR delivery, remote first aid."""

import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from medic import agent as agent_mod
from medic import dashboard, delivery, paths, project
from medic.agent import Medic
from medic.incidents import Incident


@pytest.fixture
def restore_paths():
    saved = {k: getattr(paths, k) for k in ("MEDIC_DIR", "STATE", "INCIDENTS", "EVIDENCE", "TRAFFIC", "DEPLOY",
                                           "AGENT_FILES", "CASES", "WORKFLOW", "RUNNER", "PYTHON", "PROJECT")}
    yield
    for k, v in saved.items():
        setattr(paths, k, v)


def test_medic_toml_activates_the_users_project(tmp_path, monkeypatch, restore_paths):
    (tmp_path / "medic.toml").write_text('[agent]\nentrypoint = "app.chat:answer"\nfiles = ["app/chat.py"]\n'
                                         'python = ".venv/bin/python"\n[neatlogs]\nworkflow = "docs-bot"\n'
                                         '[deploy]\nmode = "pr"\nbranch = "main"\n')
    monkeypatch.delenv("MEDIC_DIR", raising=False)
    monkeypatch.chdir(tmp_path / ".")
    proj = project.activate(project.load(project.find()))
    assert paths.DEPLOY == tmp_path.resolve() and paths.MEDIC_DIR == tmp_path.resolve() / ".medic"
    assert paths.RUNNER == ["-m", "medic.runner", "--entry", "app.chat:answer"] and paths.WORKFLOW == "docs-bot"
    assert paths.PYTHON == str(tmp_path.resolve() / ".venv/bin/python") and proj.mode == "pr"


def test_the_generic_runner_runs_any_entrypoint(tmp_path):
    (tmp_path / "bot.py").write_text("def answer(question):\n    if 'boom' in question:\n        raise ValueError('x')\n"
                                     "    return 'The pilot plant is in Narvik.'\n")
    cases = tmp_path / "cases.jsonl"
    cases.write_text(json.dumps({"id": "a", "question": "where?", "expect": [["Narvik"]]}) + "\n"
                     + json.dumps({"id": "b", "question": "boom", "expect": [["x"]]}) + "\n")
    out = tmp_path / "out.json"
    env = {"PATH": "", "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", ""), "NEATLOGS_API_KEY": ""}
    proc = subprocess.run([sys.executable, "-m", "medic.runner", "--entry", "bot:answer", "--cases", str(cases),
                           "--out", str(out), "--session", "s"], cwd=tmp_path, capture_output=True, text=True,
                          env={**__import__("os").environ, **env}, timeout=300)
    results = {c["id"]: c for c in json.loads(out.read_text())["cases"]}
    assert results["a"]["passed"] and results["a"]["status"] == "ok", proc.stderr[-500:]
    assert not results["b"]["passed"] and results["b"]["status"] == "error" and "ValueError" in results["b"]["error"]


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def test_a_release_contains_the_fix_once_it_is_deployed(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    for name in ("one", "fix", "later"):
        (tmp_path / f"{name}.txt").write_text(name)
        git(tmp_path, "add", "-A")
        git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", name)
        if name == "one":
            before = git(tmp_path, "rev-parse", "HEAD")
        if name == "fix":
            fix = git(tmp_path, "rev-parse", "HEAD")
    later = git(tmp_path, "rev-parse", "HEAD")
    assert delivery.contains(tmp_path, fix, later) and delivery.contains(tmp_path, fix, fix)
    assert not delivery.contains(tmp_path, fix, before)


@pytest.fixture
def pr_medic(tmp_path, monkeypatch, restore_paths):
    monkeypatch.setattr(paths, "MEDIC_DIR", tmp_path)
    monkeypatch.setattr(paths, "PROJECT", project.Project(root=tmp_path, entrypoint="app:answer", mode="pr"))
    monkeypatch.setattr(agent_mod.incidents, "save", lambda inc: None)
    m = Medic.__new__(Medic)
    m.log = lambda msg: None
    m.watcher = SimpleNamespace(state={"baseline": {"n": 6, "failure_rate": 0.0, "tool_error_rate": 0.0,
                                                    "non_answer_rate": 0.0, "tokens_per_run": 100,
                                                    "tool_calls_per_run": 2, "repeated_call_rate": 0.0}},
                                all_evidence=lambda: [])
    monkeypatch.setattr(m, "_neatlogs", lambda: None, raising=False)
    inc = Incident(signals=[], release="b" * 40, healthy_release="a" * 40, trace_ids=[], id="inc1", phase="remedied")
    inc.remedy = {"branch": "medic/fix-inc1", "commit": "f" * 40,
                  "attempts": [{"n": 1, "accepted": True, "commit_title": "Fix search_docs"}]}
    return m, inc


def test_pr_mode_opens_a_pr_then_waits_for_the_merge(pr_medic, monkeypatch):
    m, inc = pr_medic
    opened = []
    monkeypatch.setattr(delivery, "open_pr", lambda inc, branch, base, root, title: opened.append(
        (branch, base, title)) or {"url": "https://github.com/u/app/pull/7", "branch": branch, "base": base})
    m.do_deploy(inc)
    assert opened == [("medic/fix-inc1", "main", "Medic: Fix search_docs")] and inc.phase == "awaiting_merge"
    monkeypatch.setattr(delivery, "state", lambda url, root: {"state": "OPEN", "merge_commit": None})
    m.step(inc) if False else m.do_await_merge(inc)
    assert inc.phase == "awaiting_merge"                                 # waits as long as the review takes
    monkeypatch.setattr(delivery, "state", lambda url, root: {"state": "MERGED", "merge_commit": "m" * 40})
    monkeypatch.setattr(delivery, "fetch", lambda root: None)
    m.do_await_merge(inc)
    assert inc.phase == "deployed" and inc.deploy["release"] == "m" * 40 and inc.deploy["mode"] == "pr"


def test_pr_mode_confirms_the_heal_on_releases_that_contain_the_fix(pr_medic, monkeypatch):
    from medic.evidence import TraceEvidence
    m, inc = pr_medic
    inc.phase, inc.deploy = "deployed", {"mode": "pr", "release": "m" * 40, "at": time.time()}

    def ev(i, release, failed=False):
        return TraceEvidence(trace_id=f"t{i}", started_at="2026-10-11T00:00:00Z", release=release, status="OK",
                             cost_usd=0, tokens=100, tool_calls=2, duration_ms=1, question="q",
                             answer="" if failed else "Narvik")
    traces = [ev(i, "old000000000", failed=True) for i in range(6)] + [ev(10 + i, "new000000000") for i in range(6)]
    m.watcher.all_evidence = lambda: traces
    monkeypatch.setattr(delivery, "contains", lambda root, fix, release: release == "new000000000")
    canaries = []
    monkeypatch.setattr(agent_mod.gitops, "rev_parse", lambda rev, root: rev + "0" * 28)
    monkeypatch.setattr(m, "_release_dir", lambda sha: paths.MEDIC_DIR, raising=False)
    monkeypatch.setattr(agent_mod, "save_state", lambda state: None)
    monkeypatch.setattr(agent_mod.canary, "run", lambda state, log, code_dir, sha: canaries.append(sha) or
                        {"pass_rate": 1.0, "n": 4})
    m.do_monitor(inc)
    assert inc.phase == "resolved" and canaries == ["new000000000" + "0" * 28]   # judged on the fixed release only


def test_pr_mode_does_not_call_a_fix_healed_when_its_answers_are_wrong(pr_medic, monkeypatch):
    from medic.evidence import TraceEvidence
    m, inc = pr_medic
    inc.phase, inc.deploy = "deployed", {"mode": "pr", "release": "m" * 40, "at": time.time()}
    healthy = [TraceEvidence(trace_id=f"t{i}", started_at="2026-10-11T00:00:00Z", release="new000000000",
                             status="OK", cost_usd=0, tokens=100, tool_calls=2, duration_ms=1, question="q",
                             answer="Narvik") for i in range(6)]
    rescued = [TraceEvidence(trace_id=f"r{i}", started_at="2026-10-11T00:00:00Z", release="new000000000",
                             status="OK", cost_usd=0, tokens=100, tool_calls=2, duration_ms=1, question="q",
                             answer="Narvik", first_aid=["auto:function:search"]) for i in range(6)]
    m.watcher.all_evidence = lambda: rescued                             # only first aid kept it working
    monkeypatch.setattr(delivery, "contains", lambda root, fix, release: True)
    m.do_monitor(inc)
    assert inc.phase == "deployed"                                       # no evidence about the cure yet
    m.watcher.all_evidence = lambda: healthy                             # traffic fine, answers wrong
    monkeypatch.setattr(agent_mod.gitops, "rev_parse", lambda rev, root: rev)
    monkeypatch.setattr(m, "_release_dir", lambda sha: paths.MEDIC_DIR, raising=False)
    monkeypatch.setattr(agent_mod, "save_state", lambda state: None)
    monkeypatch.setattr(agent_mod.canary, "run", lambda state, log, code_dir, sha: {"pass_rate": 0.25, "n": 4})
    monkeypatch.setattr(delivery, "revert_pr", lambda *a: {"url": "https://github.com/u/app/pull/9"})
    m.do_monitor(inc)
    assert inc.phase == "escalated" and inc.monitor["canary"]["ok"] is False


def test_pr_mode_opens_a_revert_pr_if_the_fix_does_not_hold(pr_medic, monkeypatch):
    from medic.evidence import TraceEvidence
    m, inc = pr_medic
    inc.phase, inc.deploy = "deployed", {"mode": "pr", "release": "m" * 40, "at": time.time()}
    traces = [TraceEvidence(trace_id=f"t{i}", started_at="2026-10-11T00:00:00Z", release="new000000000",
                            status="ERROR", cost_usd=0, tokens=100, tool_calls=2, duration_ms=1, question="q",
                            answer="") for i in range(6)]
    m.watcher.all_evidence = lambda: traces
    monkeypatch.setattr(delivery, "contains", lambda root, fix, release: True)
    monkeypatch.setattr(delivery, "revert_pr", lambda inc, merge, base, root, why, worktrees:
                        {"url": "https://github.com/u/app/pull/8"})
    m.do_monitor(inc)
    assert inc.phase == "escalated" and inc.deploy["revert_pr"]["url"].endswith("/8")


@pytest.fixture
def control_server(tmp_path, monkeypatch):
    (tmp_path / "control").mkdir()
    (tmp_path / "control" / "standby.json").write_text('{"payload": {"good_release": "a"}}')
    handler = type("H", (dashboard.Handler,), {"medic_dir": tmp_path})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def fetch(url, token=None):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, b""


def test_bundles_are_served_only_with_the_fetch_token(control_server, monkeypatch):
    monkeypatch.delenv("MEDIC_CONTROL_TOKEN", raising=False)
    assert fetch(control_server + "/control/standby.json", "anything")[0] == 404     # no token configured: never
    monkeypatch.setenv("MEDIC_CONTROL_TOKEN", "fetch-secret")
    assert fetch(control_server + "/control/standby.json")[0] == 404
    assert fetch(control_server + "/control/standby.json", "wrong")[0] == 404
    status, body = fetch(control_server + "/control/standby.json", "fetch-secret")
    assert status == 200 and b"good_release" in body
    assert fetch(control_server + "/control/first_aid.json", "fetch-secret")[0] == 404   # none published


def test_detection_keeps_running_while_a_fix_waits_for_review(pr_medic, monkeypatch):
    from medic.watcher import Signal
    m, inc = pr_medic
    inc.phase = "awaiting_merge"
    monkeypatch.setattr(agent_mod.incidents, "open_incidents", lambda: [inc])
    opened = []
    m.watcher.poll = lambda: 0
    m.watcher.recent = lambda: []
    m.watcher.check = lambda: ([Signal("failure", "failure_rate", 0.0, 0.5)], {})
    m.step = lambda i: None
    for name in ("_refresh_standby",):
        monkeypatch.setattr(m, name, lambda: None, raising=False)
    monkeypatch.setattr(m, "_neatlogs_alerts", lambda: [], raising=False)
    monkeypatch.setattr(m, "_attach_alerts", lambda i, a: None, raising=False)
    monkeypatch.setattr(m, "_recently_escalated", lambda s: None, raising=False)
    monkeypatch.setattr(m, "open", lambda signals, alerts=None: opened.append(signals), raising=False)
    monkeypatch.setattr(agent_mod.canary, "due", lambda state: False)
    monkeypatch.setattr(delivery, "contains", lambda root, fix, release: False)
    monkeypatch.setattr(m, "_live_release", lambda: "b" * 40, raising=False)
    m.tick()
    assert opened == []                     # the broken release it already covers: no second incident
    monkeypatch.setattr(m, "_live_release", lambda: "c" * 40, raising=False)
    m.tick()
    assert len(opened) == 1                 # a different release broke meanwhile: a new incident


def test_delivery_errors_are_delivery_errors(tmp_path, monkeypatch):
    def missing(*a, **kw):
        raise FileNotFoundError("gh")
    monkeypatch.setattr(delivery.subprocess, "run", missing)
    with pytest.raises(delivery.DeliveryError, match="not installed"):
        delivery.state("https://github.com/u/app/pull/7", tmp_path)

    def slow(*a, **kw):
        raise subprocess.TimeoutExpired("gh", 300)
    monkeypatch.setattr(delivery.subprocess, "run", slow)
    with pytest.raises(delivery.DeliveryError, match="timed out"):
        delivery.state("https://github.com/u/app/pull/7", tmp_path)


def test_a_restart_tracks_the_pr_it_already_opened(tmp_path, monkeypatch):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd[:3])
        out = json.dumps([{"url": "https://github.com/u/app/pull/7", "state": "OPEN"}]) if cmd[:3] == ["gh", "pr", "list"] else ""
        return SimpleNamespace(returncode=0, stdout=out, stderr="")
    monkeypatch.setattr(delivery.subprocess, "run", run)
    inc = Incident(signals=[], release="b" * 40, healthy_release="a" * 40, trace_ids=[], id="inc1")
    pr = delivery.open_pr(inc, "medic/fix-inc1", "main", tmp_path, "Medic: fix")
    assert pr["url"].endswith("/7") and ["gh", "pr", "create"] not in calls


def test_a_release_deployed_before_medic_fetched_it_is_watched_after_the_fetch(tmp_path, monkeypatch):
    from medic import watcher as watcher_mod
    monkeypatch.setattr(paths, "DEPLOY", tmp_path)
    have = set()
    fetches = []

    def run(cmd, **kw):
        if cmd[:2] == ["git", "fetch"]:
            fetches.append(1)
            have.add("abc")
        return SimpleNamespace(returncode=0 if cmd[:2] == ["git", "cat-file"] and cmd[-1].split("^")[0] in have
                               else 1)
    monkeypatch.setattr(watcher_mod.subprocess, "run", run)
    w = watcher_mod.Watcher.__new__(watcher_mod.Watcher)
    assert w._ours(["release:abc"]) and fetches == [1]
    assert w._ours(["release:abc"]) and fetches == [1]                    # known now: no more fetching
    assert not w._ours(["release:zzz"])                                   # unknown and fetched a moment ago


def test_async_agents_share_one_event_loop(tmp_path):
    (tmp_path / "abot.py").write_text(
        "import asyncio\n_loop = []\n\n\n"
        "async def answer(question):\n"
        "    loop = asyncio.get_running_loop()\n"
        "    _loop.append(_loop[0] if _loop else loop)\n"
        "    if _loop[0] is not loop:\n"
        "        raise RuntimeError('client bound to another event loop')\n"   # what a pooled client does
        "    await asyncio.sleep(0.01)\n"
        "    return 'The pilot plant is in Narvik.'\n")
    cases = tmp_path / "cases.jsonl"
    cases.write_text("".join(json.dumps({"id": f"c{i}", "question": "where?", "expect": [["Narvik"]]}) + "\n"
                             for i in range(6)))
    out = tmp_path / "out.json"
    proc = subprocess.run([sys.executable, "-m", "medic.runner", "--entry", "abot:answer", "--cases", str(cases),
                           "--out", str(out), "--session", "s", "--workers", "3"], cwd=tmp_path,
                          capture_output=True, text=True, env={**__import__("os").environ, "NEATLOGS_API_KEY": ""},
                          timeout=300)
    results = json.loads(out.read_text())["cases"]
    assert all(r["passed"] for r in results), [r["error"] for r in results] or proc.stderr[-500:]


def test_a_sync_wrapper_returning_async_work_is_awaited_on_one_loop(tmp_path):
    (tmp_path / "wbot.py").write_text(
        "import asyncio\n_loop = []\n\n\n"
        "async def _answer(question):\n"
        "    loop = asyncio.get_running_loop()\n"
        "    _loop.append(_loop[0] if _loop else loop)\n"
        "    if _loop[0] is not loop:\n"
        "        raise RuntimeError('client bound to another event loop')\n"
        "    await asyncio.sleep(0.01)\n"
        "    return 'The pilot plant is in Narvik.'\n\n\n"
        "def answer(question):\n"
        "    return _answer(question)\n")                                 # sync def, async work
    cases = tmp_path / "cases.jsonl"
    cases.write_text("".join(json.dumps({"id": f"c{i}", "question": "where?", "expect": [["Narvik"]]}) + "\n"
                             for i in range(6)))
    out = tmp_path / "out.json"
    proc = subprocess.run([sys.executable, "-m", "medic.runner", "--entry", "wbot:answer", "--cases", str(cases),
                           "--out", str(out), "--session", "s", "--workers", "3"], cwd=tmp_path,
                          capture_output=True, text=True, env={**__import__("os").environ, "NEATLOGS_API_KEY": ""},
                          timeout=300)
    results = json.loads(out.read_text())["cases"]
    assert all(r["passed"] for r in results), [r["error"] or r["answer"] for r in results] or proc.stderr[-500:]
