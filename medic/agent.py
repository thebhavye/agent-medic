"""Agent Medic: watches a production agent, and diagnoses, fixes, deploys and confirms.

Each tick: poll neatlogs for new production traces, advance every open incident by one
phase, and open a new incident if the latest window drifted from the baseline. All state is
on disk, so Medic can be stopped and restarted at any point.
"""

import time
from concurrent.futures import ThreadPoolExecutor

import neatlogs

from medic import anatomy, canary, coordinator, incidents, localize, paths, remedy, triage, world
from medic.incidents import Incident
from medic.investigators import dependency, loops, regression
from medic.watcher import RECENT, Signal, Watcher, compare, save_state
from medic import evidence

MONITOR_TIMEOUT_S = 1800


class Medic:
    def __init__(self, reader, auto_deploy: bool = True, log=print) -> None:
        self.reader = reader
        self.watcher = Watcher(reader, log=log)
        self.auto_deploy = auto_deploy
        self.log = log

    def tick(self) -> None:
        added = self.watcher.poll()
        if added:
            self.log(f"[medic] {added} new production traces")
        open_now = incidents.open_incidents()
        for inc in open_now:
            self.step(inc)
        if open_now:
            return
        signals, current = self.watcher.check()
        # Wrong-but-confident answers leave no trace signal; the canary eval catches them.
        if self.watcher.state.get("baseline") and canary.due(self.watcher.state):
            canary.run(self.watcher.state, self.log)
            save_state(self.watcher.state)
            quality = canary.regressed(self.watcher.state)
            if quality:
                signals = [*signals, Signal(**quality)]
        if signals and not self._recently_escalated(signals):
            self.open(signals)

    def _recently_escalated(self, signals) -> bool:
        """Don't reopen the same problem every tick while a human handles an escalation."""
        names = {s.name for s in signals}
        for inc in reversed(incidents.load_all()):
            if inc.phase == "escalated" and time.time() - inc.opened_at < 1800 and                     names <= {s["name"] for s in inc.signals}:
                return True
        return False

    def run(self, interval: float) -> None:
        while True:
            try:
                self.tick()
            except Exception as exc:   # keep watching; the failed step is retried next tick
                self.log(f"[medic] tick failed: {type(exc).__name__}: {exc}")
            time.sleep(interval)

    # -- phases ----------------------------------------------------------------------

    def open(self, signals) -> Incident:
        recent = self.watcher.recent()
        inc = Incident(signals=[s.__dict__ for s in signals], release=world.head(),
                       healthy_release=self._healthy_release(), trace_ids=[e.trace_id for e in recent])
        inc.note("open", "; ".join(s.describe() for s in signals), self.log)
        incidents.save(inc)
        return inc

    def _healthy_release(self) -> str:
        short = self.watcher.state.get("healthy_release") or ""
        if not short:
            return ""
        try:
            return world.git("rev-parse", short)
        except Exception:
            return short

    def step(self, inc: Incident) -> None:
        handler = {"open": self.do_triage, "triaged": self.do_investigate, "investigated": self.do_decide,
                   "decided": self.do_remedy, "remedied": self.do_deploy, "deployed": self.do_monitor}[inc.phase]
        with neatlogs.trace(f"medic:{inc.phase}", kind="AGENT", session_id=f"incident-{inc.id}",
                            incident_id=inc.id, phase=inc.phase):
            handler(inc)
        incidents.save(inc)

    def do_triage(self, inc: Incident) -> None:
        by_id = {e.trace_id: e for e in self.watcher.all_evidence()}
        evs = [by_id[t] for t in inc.trace_ids if t in by_id]
        inc.triage = triage.triage(evs, inc.signals, release_changed=inc.release[:12] != inc.healthy_release[:12])
        last = (self.watcher.state.get("canary") or [{}])[-1]
        if any(s["name"] == "quality" for s in inc.signals) and last.get("failed"):
            inc.triage["examples"] = [{"question": f["question"], "answer": f["answer"], "expect": f["expect"],
                                       "tool_errors": []} for f in last["failed"][:4]]
            inc.triage["summary"] = (f"canary: {len(last['failed'])}/{last['n']} known-answer questions now answered "
                                     "wrong, while production traces show no errors")
        inc.note("triaged", f"{inc.triage['summary']}; sending {', '.join(inc.triage['investigators'])}", self.log)
        inc.phase = "triaged"

    def do_investigate(self, inc: Incident) -> None:
        by_id = {e.trace_id: e for e in self.watcher.all_evidence()}
        evs = [by_id[t] for t in inc.trace_ids if t in by_id]
        jobs = {
            "dependency": lambda: dependency.investigate(paths.KB_URL, self.watcher.state.get("contract"),
                                                         inc.triage["examples"]),
            "loops": lambda: loops.investigate(evs, self.watcher.state.get("baseline") or {}),
            "regression": lambda: regression.investigate(paths.DEPLOY, inc.healthy_release, inc.release,
                                                         inc.signals[0]["name"], paths.CASES, self.reader, self.log),
            "localize": lambda: self._localize(inc, evs),
        }
        chosen = [n for n in inc.triage["investigators"] if n in jobs]
        with ThreadPoolExecutor(max_workers=len(chosen)) as pool:
            futures = {n: pool.submit(jobs[n]) for n in chosen}
            for name, fut in futures.items():
                try:
                    inc.findings[name] = fut.result()
                except Exception as exc:
                    inc.findings[name] = {"investigator": name, "verdict": "error", "confidence": 0,
                                          "detail": f"{type(exc).__name__}: {exc}"}
                inc.note("investigated", f"{name}: {inc.findings[name]['verdict']}: {inc.findings[name]['detail']}",
                         self.log)
        inc.phase = "investigated"

    def _localize(self, inc: Incident, evs: list) -> dict:
        """Diff the failing window against healthy traces of the last good release, then map the
        suspects onto the deployed code."""
        healthy_release = inc.healthy_release[:12]
        healthy = [e for e in self.watcher.all_evidence()
                   if e.release == healthy_release and not e.failed and e.trace_id not in inc.trace_ids][-12:]
        failing = [e for e in evs if e.failed or e.repeated_calls or e.tool_errors] or evs
        result = localize.localize(failing, healthy)
        files = [f for f in paths.AGENT_FILES if (paths.DEPLOY / f).exists()]
        result["surfaces"] = anatomy.resolve(anatomy.scan(paths.DEPLOY, files), result.get("suspects", []))[:6]
        for s in result["surfaces"][:2]:
            if s["kind"] in ("tool_handler", "client") and s.get("symbol"):
                s["impact"] = anatomy.graph_impact(paths.DEPLOY, s["symbol"], s["file"])
        return {"investigator": "localize", **result}

    def do_decide(self, inc: Incident) -> None:
        inc.decision = coordinator.decide(inc.triage, inc.findings)
        d = inc.decision
        inc.note("decided", f"cause {d['cause']} (confidence {d['confidence']}), remedy {d['remedy']}", self.log)
        for note in d["disagreements"]:
            inc.note("decided", f"settled: {note}", self.log)
        inc.phase = "escalated" if d["remedy"] == "escalate" else "decided"
        if inc.phase == "escalated":
            inc.note("escalated", "no safe automatic fix for this cause; paging a human", self.log)

    def do_remedy(self, inc: Incident) -> None:
        record = remedy.attempt(inc, self.reader, self.log)
        inc.remedy.setdefault("attempts", []).append(record)
        inc.note("remedy", f"attempt {record['n']}: {'accepted' if record['accepted'] else 'rejected'}: "
                           f"{record.get('feedback', '')}", self.log)
        if record["accepted"]:
            inc.remedy.update(branch=record["branch"], commit=record["commit"])
            inc.phase = "remedied"
        elif record["n"] >= remedy.MAX_ATTEMPTS:
            inc.phase = "escalated"
            inc.note("escalated", f"no verified fix after {remedy.MAX_ATTEMPTS} attempts", self.log)

    def do_deploy(self, inc: Incident) -> None:
        if not self.auto_deploy:
            if not inc.deploy.get("approved"):
                return       # waiting for `agent-medic approve <id>`
        inc.deploy = {**inc.deploy, **remedy.deploy(inc), "at": time.time()}
        if inc.deploy["deployed"]:
            inc.note("deployed", f"release {inc.deploy['release'][:8]} is live", self.log)
            inc.phase = "deployed"
        else:
            inc.note("escalated", inc.deploy["detail"], self.log)
            inc.phase = "escalated"

    def do_monitor(self, inc: Incident) -> None:
        release = inc.deploy["release"][:12]
        evs = self.watcher.recent(release=release)
        if len(evs) < RECENT:
            if time.time() - inc.deploy["at"] > MONITOR_TIMEOUT_S:
                inc.note("escalated", f"only {len(evs)} production traces on the fixed release after "
                                      f"{MONITOR_TIMEOUT_S // 60} minutes", self.log)
                inc.phase = "escalated"
            return
        current = evidence.window(evs)
        signals = compare(self.watcher.state["baseline"], current)
        inc.monitor = {"window": current, "signals": [s.__dict__ for s in signals]}
        if signals:
            inc.note("escalated", "production is still unhealthy on the fixed release: "
                                  + "; ".join(s.describe() for s in signals), self.log)
            inc.phase = "escalated"
        else:
            inc.note("resolved", f"production healthy on release {release[:8]}: failure rate "
                                 f"{current['failure_rate']:.0%} over {current['n']} traces", self.log)
            inc.phase = "resolved"
