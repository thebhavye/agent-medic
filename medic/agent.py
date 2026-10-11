"""Agent Medic: watches a production agent, and diagnoses, fixes, deploys and confirms.

Each tick: poll neatlogs for new production traces, advance every open incident by one
phase, and open a new incident if the latest window drifted from the baseline. All state is
on disk, so Medic can be stopped and restarted at any point.
"""

import os
import time
from concurrent.futures import ThreadPoolExecutor

import neatlogs

from bisect_agent import gitops
from medic import (anatomy, canary, coordinator, delivery, first_aid, incidents, localize, neatlogs_link, paths,
                   project, remedy,
                   rollout, triage, world)
from medic.incidents import Incident
from medic.investigators import dependency, loops, regression
from medic.watcher import RECENT, Signal, Watcher, compare, save_state
from medic import evidence

MONITOR_TIMEOUT_S = 1800
ROLLOUT = os.environ.get("MEDIC_ROLLOUT", "canary")    # canary | direct


class Medic:
    def __init__(self, reader, auto_deploy: bool = True, log=print, alerts_since: float | None = None) -> None:
        self.reader = reader
        self.watcher = Watcher(reader, log=log)
        self.auto_deploy = auto_deploy
        self.log = log
        # neatlogs alerts are read from the saved checkpoint, so a restart misses none; a fresh
        # world (a benchmark run) passes its start, and earlier worlds' alerts are ignored.
        self.alerts_since = alerts_since

    def tick(self) -> None:
        added = self.watcher.poll()
        if added:
            self.log(f"[medic] {added} new production traces")
        self._refresh_standby()
        open_now = incidents.open_incidents()
        if open_now:
            # neatlogs may raise the same incident a moment after Medic opened it: attach its
            # alert to the open incident, so both detectors' accounts meet there.
            self._attach_alerts(open_now[-1], self._neatlogs_alerts())
        for inc in open_now:
            self.step(inc)
        open_now = incidents.open_incidents()
        if any(not self._waiting_on_user(i) for i in open_now):
            return
        # Every open incident's cure is with the user (in review, or merged and not yet deployed),
        # which can take days: keep watching for anything else that breaks meanwhile.
        waiting = open_now
        signals, current = self.watcher.check()
        # The runtime healing a request by itself is an incident too: the code is still broken.
        recent = self.watcher.recent()
        healed = [e for e in recent if any(f.startswith("auto:") for f in e.first_aid)]
        if healed:
            signals = [*signals, Signal("self_healed", "self_healed_rate", 0.0, len(healed) / len(recent))]
        # Wrong-but-confident answers leave no trace signal; the canary eval catches them.
        if self.watcher.state.get("baseline") and canary.due(self.watcher.state):
            live = self._live_release()
            canary.run(self.watcher.state, self.log, code_dir=self._release_dir(live), sha=live)
            save_state(self.watcher.state)
            quality = canary.regressed(self.watcher.state)
            if quality:
                signals = [*signals, Signal(**quality)]
        # neatlogs' detections and alert rules watch every span as it arrives: a fired alert is
        # the earliest, best-grounded signal Medic gets.
        alerts = self._neatlogs_alerts()
        # Each alert keeps its rule's identity, so one rule's escalation never hides another's.
        signals = [*signals, *(Signal(f"neatlogs_alert:{a.get('ruleName') or a.get('ruleId')}", "neatlogs_alert_hits",
                                      float(a.get("threshold") or 0), float(a.get("count") or 0)) for a in alerts)]
        if signals and waiting:
            live = self._live_release()
            covering = next((i for i in waiting if self._covers(i, live)), None)
            if covering is not None:     # the same broken release: its incident already has it
                if alerts:
                    self._attach_alerts(covering, alerts)
                signals, alerts = [], []
        if signals:
            escalated = self._recently_escalated(signals)
            if escalated is None:
                self.open(signals, alerts)
            elif alerts:
                # Same problem a human is already handling: record the alert there, not nowhere.
                self._attach_alerts(escalated, alerts)
        # check() may just have recorded the healthy release: arm the runtime now, not a tick
        # later, so a release that breaks right away already meets an armed runtime.
        self._refresh_standby()

    # -- neatlogs ------------------------------------------------------------------------

    def _neatlogs(self):
        """The neatlogs API for writes, once `agent-medic neatlogs setup` has run; else None."""
        if not neatlogs_link.load().get("rules"):
            return None
        if getattr(self, "_nl", None) is None:
            self._nl = neatlogs_link.Api()
        return self._nl

    def _neatlogs_alerts(self) -> list[dict]:
        api = self._neatlogs()
        if api is None:
            return []
        try:
            alerts = neatlogs_link.new_alerts(api, since=getattr(self, "alerts_since", None))
        except Exception as exc:  # noqa: BLE001 - neatlogs down: Medic's own signals still work
            self.log(f"[neatlogs] alert history unavailable: {type(exc).__name__}: {exc}")
            return []
        # Alert rules see the whole neatlogs project, which can hold other agents and other
        # deployments. An alert counts only if its own sample traces show this deployment failing.
        mine = []
        for alert in alerts:
            samples = self.watcher.evidence_for(alert.get("sampleTraceIds") or [])
            if samples:
                mine.append(alert)
            else:
                self.log(f"[neatlogs] alert '{alert.get('ruleName')}' ignored: none of its sample traces are "
                         f"production traces of '{paths.WORKFLOW}' from this repo's releases")
        return mine

    def _attach_alerts(self, inc: Incident, alerts: list[dict]) -> None:
        if not alerts:
            return
        first = alerts[0]
        samples = [t for a in alerts for t in a.get("sampleTraceIds") or []]
        if not inc.neatlogs.get("alert"):
            inc.neatlogs["alert"] = {k: first.get(k) for k in ("id", "ruleId", "ruleName", "detectionName", "firedAt",
                                                               "count", "threshold", "windowMinutes", "investigationTarget")}
        inc.neatlogs["alerts"] = inc.neatlogs.get("alerts", 0) + len(alerts)
        inc.neatlogs["sample_trace_ids"] = list(dict.fromkeys((inc.neatlogs.get("sample_trace_ids") or []) + samples))[:10]
        inc.note("neatlogs", "neatlogs raised it too: " + "; ".join(
            f"{a.get('ruleName')} ({a.get('count')} hits)" for a in alerts), self.log)
        incidents.save(inc)

    def _evidence(self, inc: Incident) -> list:
        """The incident's traces: neatlogs' alert samples first (the failures neatlogs saw), then
        the window that opened it. Samples the watcher hasn't read are fetched from neatlogs."""
        ids = list(dict.fromkeys((inc.neatlogs.get("sample_trace_ids") or []) + inc.trace_ids))
        return self.watcher.evidence_for(ids)

    def _card_id(self, inc: Incident) -> str | None:
        return ((inc.findings.get("neatlogs") or {}).get("suggestion_id")
                or (inc.neatlogs.get("card") or {}).get("suggestion_id"))

    def _tell_neatlogs(self, inc: Incident, text: str, resolved: bool = False, **sections) -> None:
        """Record what Medic did, and put its proven account on neatlogs' fix card for this
        incident: each milestone appends a version, so the card's history shows the healing."""
        if self._neatlogs() is None:
            return
        notes = inc.neatlogs.setdefault("notes", [])
        notes.append({"at": time.time(), "text": text, "card": False})
        # The proven root cause and the shipped fix wait here until a card takes them: neatlogs
        # often creates its card after Medic's diagnosis, and that card should still get them.
        pending = inc.neatlogs.setdefault("pending", {})
        pending.update({k: v for k, v in sections.items() if v})
        self._late_neatlogs_card(inc)
        card = self._card_id(inc)
        if card:
            evidence = (f"**Agent Medic**, incident `{inc.id}`: what was proven and done\n\n"
                        + "\n".join(f"- {time.strftime('%H:%M:%S', time.localtime(n['at']))} {n['text']}" for n in notes))
            if neatlogs_link.update_card(card, evidence_reasoning=evidence, **pending):
                notes[-1]["card"] = True
                pending.clear()
        if resolved and card and neatlogs_link.update_fix(card, "resolved", note=f"Agent Medic: {text}"):
            inc.neatlogs["suggestion_resolved"] = card

    def _late_neatlogs_card(self, inc: Incident) -> None:
        """neatlogs investigates on its own schedule, often after Medic's diagnosis: look again,
        and attach its card so the two accounts of the incident meet."""
        if (inc.findings.get("neatlogs") or {}).get("verdict") == "root_cause" or inc.neatlogs.get("card"):
            return
        traces = list(dict.fromkeys((inc.neatlogs.get("sample_trace_ids") or []) + inc.trace_ids))
        card = neatlogs_link.investigate(traces, inc.opened_at)
        if card.get("verdict") == "root_cause":
            inc.neatlogs["card"] = card
            inc.note("neatlogs", f"neatlogs' investigation arrived: \"{card['detail']}\" "
                                 f"({card['confidence']:.0%}); Medic's diagnosis: "
                                 f"{inc.decision.get('cause', '').replace('_', ' ')}", self.log)

    # -- the user's project (medic.toml) -----------------------------------------------

    def _pr_mode(self) -> bool:
        """A user's project whose fixes arrive as pull requests: Medic doesn't own its deploys."""
        proj = project.current()
        return bool(proj and proj.mode == "pr")

    @staticmethod
    def _waiting_on_user(inc: Incident) -> bool:
        """The cure is a PR in review, or merged and waiting for the user's deploy."""
        return inc.phase in ("pr_ready", "awaiting_merge") or (inc.phase == "deployed"
                                                               and inc.deploy.get("mode") == "pr")

    def _covers(self, inc: Incident, live: str) -> bool:
        """Is `live` the release this incident is about (the broken one, or one carrying its fix)?"""
        if live[:12] == (inc.release or "")[:12]:
            return True
        merged = (inc.deploy or {}).get("release")
        return bool(merged) and delivery.contains(paths.DEPLOY, merged, live)

    def _live_release(self) -> str:
        """What production runs. In the demo world, the deploy repo's HEAD. In a user's project,
        the release tag on the newest production traces (their deploy sets GIT_SHA)."""
        if not self._pr_mode():
            return world.head()
        recent = self.watcher.recent()
        tag = recent[-1].release if recent else ""
        delivery.fetch(paths.DEPLOY)
        try:
            return gitops.rev_parse(tag, paths.DEPLOY) if tag else gitops.rev_parse("HEAD", paths.DEPLOY)
        except Exception:  # noqa: BLE001 - a release Medic can't resolve: fall back to the checkout
            return gitops.rev_parse("HEAD", paths.DEPLOY)

    def _release_dir(self, sha: str):
        """A clean checkout of a release to run evals in: the deploy repo itself in the demo world,
        a cached worktree in a user's project (their own checkout is never used or touched)."""
        if not self._pr_mode():
            return paths.DEPLOY
        work = paths.MEDIC_DIR / "worktrees" / f"release-{sha[:12]}"
        if not (work / ".git").exists():
            gitops.worktree_add(work, sha, paths.DEPLOY)
        return work

    def _refresh_standby(self) -> None:
        """Keep the runtime armed: the last known good of every registered part, from the
        healthy release, ready for the runtime to engage by itself."""
        healthy = self.watcher.state.get("healthy_release")
        if not healthy:
            return
        try:
            live = self._live_release()
            current = first_aid.current_standby()
            # Rebuilt when the healthy release moves, when a new release goes live (it may
            # register parts the old standby doesn't cover), and well before it expires.
            fresh = (current and current.get("good_release") == healthy[:12]
                     and current.get("live_release") == live[:12]
                     and time.time() - float(current.get("issued_at") or 0) < first_aid.STANDBY_TTL_S / 2)
            if fresh:
                return
            body = first_aid.standby(paths.DEPLOY, world.git("rev-parse", healthy), live,
                                     baseline=self.watcher.state.get("baseline"))
            first_aid.write(first_aid.standby_path(), body)
            self.log(f"[medic] runtime armed: last known good of {len(body['functions']) + len(body['values'])} "
                     f"parts from {healthy[:8]}")
        except Exception as exc:  # noqa: BLE001 - arming is best effort; detection still works
            self.log(f"[medic] could not arm the runtime: {type(exc).__name__}: {exc} ({_where(exc)})")

    def _recently_escalated(self, signals) -> Incident | None:
        """Don't reopen the same problem every tick while a human handles an escalation: the
        escalated incident that already covers these signals, or None."""
        names = {s.name for s in signals}
        for inc in reversed(incidents.load_all()):
            if inc.phase == "escalated" and time.time() - inc.opened_at < 1800 and                     names <= {s["name"] for s in inc.signals}:
                return inc
        return None

    def run(self, interval: float) -> None:
        while True:
            try:
                self.tick()
            except Exception as exc:   # keep watching; the failed step is retried next tick
                self.log(f"[medic] tick failed: {type(exc).__name__}: {exc} ({_where(exc)})")
            self._sleep(interval)

    def _sleep(self, interval: float) -> None:
        """Sleep until the next tick, or until neatlogs rings the doorbell (an alert fired)."""
        bell = neatlogs_link.doorbell()
        rung = bell.stat().st_mtime if bell.exists() else 0.0
        deadline = time.time() + interval
        while time.time() < deadline:
            time.sleep(0.5)
            if bell.exists() and bell.stat().st_mtime != rung:
                self.log("[neatlogs] alert webhook: waking up")
                return

    # -- phases ----------------------------------------------------------------------

    def open(self, signals, alerts: list[dict] | None = None) -> Incident:
        recent = self.watcher.recent()
        inc = Incident(signals=[s.__dict__ for s in signals], release=self._live_release(),
                       healthy_release=self._healthy_release(), trace_ids=[e.trace_id for e in recent])
        if alerts:
            # neatlogs' alert is the incident's origin: its rule, its detection, its traces.
            first = alerts[0]
            samples = [t for a in alerts for t in a.get("sampleTraceIds") or []]
            inc.neatlogs = {"alert": {k: first.get(k) for k in ("id", "ruleId", "ruleName", "detectionName", "firedAt",
                                                               "count", "threshold", "windowMinutes",
                                                               "investigationTarget")},
                            "alerts": len(alerts), "sample_trace_ids": samples[:10]}
            inc.note("open", f"neatlogs alert: {first.get('ruleName')} ({first.get('count')} hits"
                             + (f" in {first['windowMinutes']} min" if first.get("windowMinutes") else "") + ")", self.log)
        inc.note("open", "; ".join(s.describe() for s in signals), self.log)
        # Did the runtime already step in on its own? Trust its shared state for whether it still
        # is (it may have released a standby that didn't help); the trace spans name the parts.
        engagement = first_aid.runtime_engagement(inc.release) or {}
        if engagement.get("status") == "engaged":
            healed = sorted(engagement.get("names") or [])
            inc.first_aid = {"status": "auto", "what": ", ".join(healed) + " from the last known good release",
                             "functions": healed, "base": engagement.get("good_release"),
                             "applied_at": float(engagement.get("at") or time.time())}
            inc.note("first_aid", f"the runtime healed requests by itself: running {', '.join(healed)} "
                                  "from the last known good release", self.log)
        elif engagement.get("status") == "not_code":
            inc.note("first_aid", "the runtime tried the last known good and released it: the cause isn't in "
                                  "the parts that changed", self.log)
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
                   "decided": self.do_remedy, "remedied": self.do_deploy, "canary": self.do_canary,
                   "pr_ready": self.do_pr_ready, "awaiting_merge": self.do_await_merge,
                   "deployed": self.do_monitor}[inc.phase]
        with neatlogs.trace(f"medic:{inc.phase}", kind="AGENT", session_id=f"incident-{inc.id}",
                            incident_id=inc.id, phase=inc.phase):
            handler(inc)
        incidents.save(inc)

    def do_triage(self, inc: Incident) -> None:
        evs = self._evidence(inc)
        inc.triage = triage.triage(evs, inc.signals, release_changed=inc.release[:12] != inc.healthy_release[:12])
        last = (self.watcher.state.get("canary") or [{}])[-1]
        if any(s["name"] == "quality" for s in inc.signals) and last.get("failed"):
            inc.triage["examples"] = [{"question": f["question"], "answer": f["answer"], "expect": f["expect"],
                                       "tool_errors": []} for f in last["failed"][:4]]
            canary_line = f"canary: {len(last['failed'])}/{last['n']} known-answer questions now answered wrong"
            # Claim "no errors" only when the traces themselves show none: no failing trace in the
            # evidence and no neatlogs alert. Otherwise keep the trace account and add the canary's.
            traces_failed = any(e.failed or e.tool_errors for e in evs) or bool(inc.neatlogs.get("alert")) or any(
                s["name"] in ("tool_errors", "failures") or s["name"].startswith("neatlogs_alert") for s in inc.signals)
            if traces_failed:
                inc.triage["summary"] = f"{inc.triage['summary']}; {canary_line}"
            else:
                inc.triage["summary"] = f"{canary_line}, while production traces show no errors"
        if self._neatlogs() is not None:
            inc.triage["investigators"].append("neatlogs")    # neatlogs' own investigation, as evidence
        inc.note("triaged", f"{inc.triage['summary']}; sending {', '.join(inc.triage['investigators'])}", self.log)
        inc.phase = "triaged"

    def do_investigate(self, inc: Incident) -> None:
        evs = self._evidence(inc)
        jobs = {
            "dependency": lambda: dependency.investigate(paths.KB_URL, self.watcher.state.get("contract"),
                                                         inc.triage["examples"]),
            "loops": lambda: loops.investigate(evs, self.watcher.state.get("baseline") or {}),
            "regression": lambda: regression.investigate(paths.DEPLOY, inc.healthy_release, inc.release,
                                                         inc.signals[0]["name"], paths.CASES, self.reader, self.log),
            "localize": lambda: self._localize(inc, evs),
            "neatlogs": lambda: neatlogs_link.investigate(
                list(dict.fromkeys((inc.neatlogs.get("sample_trace_ids") or []) + inc.trace_ids)), inc.opened_at),
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
        culprit = f"; culprit commit {d['culprit'][:8]}" if d.get("culprit") else ""
        reg = inc.findings.get("regression") or {}
        proven = (f"**Proven by Agent Medic** ({d['confidence']:.0%}): {d['cause'].replace('_', ' ')}."
                  + (f" The first bad commit is `{d['culprit'][:8]}` \"{reg.get('culprit_subject', '')}\": the release "
                     f"before it passes the known-answer eval against today's dependencies and this one fails it."
                     if d.get("culprit") else "")
                  + "\n\n" + "\n".join(f"- {r}" for r in d.get("rationale", [])))
        self._tell_neatlogs(inc, f"diagnosed {d['cause'].replace('_', ' ')} ({d['confidence']:.0%} confidence){culprit}; "
                                 + ("no safe automatic fix, a human is paged" if inc.phase == "escalated"
                                    else f"remedy: {d['remedy'].replace('_', ' ')}"), root_cause=proven)
        if inc.phase == "escalated":
            inc.note("escalated", "no safe automatic fix for this cause; paging a human", self.log)
        elif d.get("cause") == "code_regression":
            self._first_aid(inc)

    def _first_aid(self, inc: Incident) -> None:
        """Serve users with the culprit's healthy parts while the cure is made (medic/first_aid.py).

        Best effort: whatever fails here (planning, the trial eval, publishing) is recorded and the
        incident moves on to the cure. First aid must never hold the cure back."""
        try:
            self._attempt_first_aid(inc)
        except Exception as exc:  # noqa: BLE001
            inc.first_aid = {**inc.first_aid, "status": "error", "reason": f"{type(exc).__name__}: {exc}"}
            inc.note("first_aid", f"first aid failed ({type(exc).__name__}); going on to the cure", self.log)

    def _attempt_first_aid(self, inc: Incident) -> None:
        p = first_aid.plan(paths.DEPLOY, inc.release, inc.healthy_release, inc.decision.get("culprit"))
        if first_aid.empty(p):
            reason = "; ".join(p["skipped"]) or "the culprit changed nothing the agent registered with the runtime"
            inc.first_aid = {"status": "not_possible", "reason": reason, "registered": p["registered"]}
            inc.note("first_aid", f"no first aid: {reason}; users wait for the cure", self.log)
            return
        body = first_aid.payload(inc.id, inc.release, p)
        what = first_aid.describe(p)
        # Prove it first: the known-answer eval on the broken release, with the bandage on.
        trial = first_aid.write(paths.MEDIC_DIR / "control" / f"trial-{inc.id}.json", body)
        try:
            record = canary.evaluate(self._release_dir(inc.release), inc.release, first_aid=trial, tag="first-aid")
        finally:
            trial.unlink(missing_ok=True)
        ok, detail = canary.answers_hold(record, self.watcher.state)
        ok = ok and record.get("error_rate", 1.0) <= 0.25
        before = next((r for r in reversed(self.watcher.state.get("canary") or [])
                       if r.get("release") == inc.release[:12] and "pass_rate" in r), None)
        if before is None:      # e.g. opened on trace errors: measure the broken release bare, for the comparison
            before = canary.evaluate(self._release_dir(inc.release), inc.release, tag="first-aid-before")
            before = before if "pass_rate" in before else None
        inc.first_aid = {"what": what, "base": p["base"], "functions": sorted(p["functions"]),
                         "values": sorted(p["values"]), "skipped": p["skipped"],
                         "trial": {k: record.get(k) for k in ("pass_rate", "error_rate", "n", "tool_calls_per_run",
                                                               "tokens_per_run", "error")},
                         "before": {k: before.get(k) for k in ("pass_rate", "error_rate", "n", "tool_calls_per_run")}
                         if before else None}
        if not ok:
            inc.first_aid.update(status="rejected", reason=detail)
            inc.note("first_aid", f"first aid not applied: with {what}, {detail}", self.log)
            return
        first_aid.publish(body)
        self._tell_neatlogs(inc, f"first aid live: {what}; known-answer eval {record['pass_rate']:.0%} with it")
        inc.first_aid.update(status="active", applied_at=time.time(), expires_at=body["expires_at"])
        before_txt = f"{before['pass_rate']:.0%} -> " if before else ""
        inc.note("first_aid", f"first aid live on {inc.release[:8]}: {what}; known-answer eval {before_txt}"
                              f"{record['pass_rate']:.0%} with it; users are served while the cure is made", self.log)

    def _remove_first_aid(self, inc: Incident, why: str) -> None:
        if first_aid.clear(inc.id):
            inc.first_aid.update(status="removed", removed_at=time.time())
            inc.note("first_aid", f"first aid removed: {why}", self.log)

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
        if self._pr_mode():
            self._open_fix_pr(inc)
            return
        if not self.auto_deploy:
            if not incidents.approved(inc):
                return       # waiting for `agent-medic approve <id>`
        if ROLLOUT == "direct":
            self._promote(inc)
            return
        active = rollout.current()
        if active and active.get("incident") == inc.id:
            # This incident's canary already started (e.g. Medic crashed before saving the phase):
            # resume judging it rather than treating it as permission to promote.
            inc.deploy = {**inc.deploy, "canary": active, "at": inc.deploy.get("at") or active["started"]}
            inc.note("canary", f"resuming the canary of candidate {active['candidate'][:8]}", self.log)
            inc.phase = "canary"
            return
        if active:
            return                    # another incident's canary is running; one rollout at a time
        state = rollout.start(inc.id, inc.remedy["branch"])
        inc.deploy = {**inc.deploy, "canary": state, "at": time.time()}
        inc.note("canary", f"candidate {state['candidate'][:8]} serves {state['share']:.0%} of production; "
                           f"{rollout.MIN_TRACES} canary traces decide promote or roll back", self.log)
        inc.phase = "canary"

    def do_canary(self, inc: Incident) -> None:
        active = rollout.current()
        if not active or active.get("incident") != inc.id:
            # The rollout state is gone or isn't ours: don't promote without a canary.
            inc.note("canary", "no canary rollout for this incident is active; starting one", self.log)
            inc.phase = "remedied"
            return
        verdict = rollout.judge(self.watcher, compare)
        if verdict is None:
            return                    # waiting for canary traffic
        if verdict["verdict"] == "healthy":
            # Clean traces don't prove right answers: a confidently wrong fix looks clean. The
            # candidate also has to pass the known-answer eval before it reaches everyone.
            try:
                record = canary.run(self.watcher.state, self.log, code_dir=rollout.root(), sha=active["candidate"])
            except Exception as e:  # noqa: BLE001 - a crashed or timed-out eval must not leave the canary serving
                verdict = {**verdict, "verdict": "inconclusive",
                           "detail": f"the known-answer eval on the candidate failed ({type(e).__name__}: {e})"}
            else:
                save_state(self.watcher.state)
                ok, detail = canary.answers_hold(record, self.watcher.state)
                verdict = {**verdict, "answers": detail}
                if not ok:
                    verdict.update(verdict="regressed", detail=f"{verdict['detail']}, but {detail}")
        inc.deploy["canary_verdict"] = {k: v for k, v in verdict.items() if k != "state"}
        if verdict["verdict"] == "healthy":
            inc.note("canary", f"canary healthy: {verdict['detail']}; {verdict['answers']}; promoting", self.log)
            rollout.finish()
            self._promote(inc)
            return
        rollout.finish()
        last = inc.remedy["attempts"][-1]
        if verdict["verdict"] == "regressed":
            last.update(accepted=False, feedback=f"rolled back: in production the canary regressed: {verdict['detail']}")
            inc.note("canary", f"rolled back automatically: {verdict['detail']}", self.log)
            inc.phase = "escalated" if len(inc.remedy["attempts"]) >= remedy.MAX_ATTEMPTS else "decided"
            return
        # Inconclusive: production never judged the fix. Never ship it on silence.
        last.update(feedback=f"rolled back: the canary was inconclusive ({verdict['detail']})")
        inc.note("escalated", f"canary inconclusive ({verdict['detail']}); rolled back, a human decides", self.log)
        inc.phase = "escalated"

    def _promote(self, inc: Incident) -> None:
        inc.deploy = {**inc.deploy, **remedy.deploy(inc), "at": time.time()}
        if inc.deploy["deployed"]:
            inc.note("deployed", f"release {inc.deploy['release'][:8]} is live", self.log)
            accepted = next((a for a in reversed(inc.remedy.get("attempts", [])) if a.get("accepted")), {})
            diff = "\n".join(f"```diff\n# {e['path']}\n" + "\n".join(f"- {l}" for l in e["find"].splitlines())
                             + "\n" + "\n".join(f"+ {l}" for l in e["replace"].splitlines()) + "\n```"
                             for e in accepted.get("edits", []))
            action = (f"**Shipped by Agent Medic**: \"{accepted.get('commit_title', '')}\" (release "
                      f"`{inc.deploy['release'][:8]}`), verified on {(accepted.get('metrics') or {}).get('n', '?')} runs at "
                      f"{(accepted.get('metrics') or {}).get('pass_rate', 0):.0%} and promoted after a healthy canary."
                      f"\n\n{accepted.get('diagnosis', '')}\n\n{diff}")
            self._tell_neatlogs(inc, f"fix promoted after a healthy canary: release {inc.deploy['release'][:8]}"
                                     + (f" \"{accepted['commit_title']}\"" if accepted.get("commit_title") else ""),
                                recommended_action_md=action)
            inc.phase = "deployed"
            # The bundle is bound to the broken release, so the cure already runs without it;
            # removing it makes that explicit, and monitoring judges the cure on its own.
            self._remove_first_aid(inc, "the cure is live")
        else:
            inc.note("escalated", inc.deploy["detail"], self.log)
            inc.phase = "escalated"

    # -- delivery as a pull request (a user's project) ------------------------------------

    def _open_fix_pr(self, inc: Incident) -> None:
        """The verified fix goes to the user's repo as a PR with the evidence; they merge and
        deploy as usual. First aid (if any) keeps users served in the meantime."""
        proj = project.current()
        accepted = next((a for a in reversed(inc.remedy.get("attempts", [])) if a.get("accepted")), {})
        title = f"Medic: {accepted.get('commit_title') or 'fix for incident ' + inc.id}"
        if proj.pr == "ready" and not incidents.approved(inc):
            try:
                ready = delivery.prepare_pr(inc, inc.remedy["branch"], proj.branch, title, paths.MEDIC_DIR / "prs",
                                            paths.DEPLOY)
            except delivery.DeliveryError as exc:
                inc.note("escalated", f"could not prepare the fix PR ({exc}); the fix is on branch "
                                      f"{inc.remedy['branch']}", self.log)
                inc.phase = "escalated"
                return
            inc.deploy = {**inc.deploy, "mode": "pr", "ready": ready, "at": time.time()}
            inc.note("remedy", f"fix PR ready, not opened: review {ready['body_file']}, then open it with "
                               f"`agent-medic approve {inc.id}` (or: {' && '.join(ready['commands'])})", self.log)
            self._tell_neatlogs(inc, f"fix ready as a pull request on branch {ready['branch']}, waiting for a "
                                     "human to open it")
            inc.phase = "pr_ready"
            return
        try:
            pr = delivery.open_pr(inc, inc.remedy["branch"], proj.branch, paths.DEPLOY, title=title)
        except delivery.DeliveryError as exc:
            inc.note("escalated", f"could not open the fix PR ({exc}); the fix is on branch "
                                  f"{inc.remedy['branch']}", self.log)
            inc.phase = "escalated"
            return
        inc.deploy = {**inc.deploy, "mode": "pr", "pr": pr, "at": time.time()}
        inc.note("remedy", f"fix PR opened: {pr['url']}; waiting for it to be merged and deployed", self.log)
        self._tell_neatlogs(inc, f"fix proposed as a pull request: {pr['url']}")
        inc.phase = "awaiting_merge"

    def do_pr_ready(self, inc: Incident) -> None:
        """The prepared PR waits for the user: `agent-medic approve` has Medic open it; opening it
        by hand with the printed commands works too, and Medic picks it up."""
        if incidents.approved(inc):
            self._open_fix_pr(inc)
            return
        if time.time() - inc.deploy.get("looked_at", 0) < 60:
            return                       # looking on GitHub once a minute is plenty
        inc.deploy["looked_at"] = time.time()
        try:
            # Closed ones too: a PR opened and closed between checks still ends this wait (escalated).
            url = delivery.existing_pr(inc.deploy["ready"]["branch"], paths.DEPLOY, closed=True)
        except delivery.DeliveryError as exc:
            self.log(f"[medic] could not look for the fix PR: {exc}")
            return
        if url:
            inc.deploy = {**inc.deploy, "pr": {"url": url, "branch": inc.deploy["ready"]["branch"]}}
            inc.note("remedy", f"fix PR opened by hand: {url}; following it to merge and deploy", self.log)
            self._tell_neatlogs(inc, f"fix proposed as a pull request: {url}")
            inc.phase = "awaiting_merge"

    def do_await_merge(self, inc: Incident) -> None:
        pr = inc.deploy.get("pr") or {}
        try:
            st = delivery.state(pr["url"], paths.DEPLOY)
        except (delivery.DeliveryError, KeyError) as exc:
            self.log(f"[medic] could not read the fix PR: {exc}")
            return
        if st["state"] == "MERGED" and st["merge_commit"]:
            delivery.fetch(paths.DEPLOY)
            inc.deploy = {**inc.deploy, "release": st["merge_commit"], "merged_at": time.time(), "at": time.time(),
                          "deployed": True}
            inc.note("deployed", f"fix PR merged ({st['merge_commit'][:8]}); watching production for a release "
                                 "that contains it", self.log)
            inc.phase = "deployed"
        elif st["state"] == "CLOSED":
            inc.note("escalated", f"the fix PR was closed without merging: {pr['url']}", self.log)
            inc.phase = "escalated"

    def _fixed_traces(self, inc: Incident) -> list:
        """Production traces from releases that contain the merged fix (the user deploys it when
        they choose; any later release carries it too)."""
        merged = inc.deploy.get("release", "")
        releases = {}
        out = []
        for e in self.watcher.all_evidence():
            if e.first_aid:
                continue                 # first aid answered it: no evidence about the cure
            if e.release not in releases:
                releases[e.release] = delivery.contains(paths.DEPLOY, merged, e.release)
            if releases[e.release]:
                out.append(e)
        return out[-RECENT:]

    def do_monitor(self, inc: Incident) -> None:
        if inc.deploy.get("mode") == "pr":
            self._monitor_pr(inc)
            return
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
            self._tell_neatlogs(inc, "still unhealthy after the fix shipped; escalated to a human")
        else:
            inc.note("resolved", f"production healthy on release {release[:8]}: failure rate "
                                 f"{current['failure_rate']:.0%} over {current['n']} traces", self.log)
            inc.phase = "resolved"
            self._tell_neatlogs(inc, f"healed: release {release[:8]} healthy in production, failure rate "
                                     f"{current['failure_rate']:.0%} over {current['n']} traces", resolved=True)

    PR_DEPLOY_WAIT_S = 7 * 24 * 3600     # the user deploys when they choose; Medic keeps watching

    def _monitor_pr(self, inc: Incident) -> None:
        evs = self._fixed_traces(inc)
        if len(evs) < RECENT:
            if time.time() - inc.deploy["at"] > self.PR_DEPLOY_WAIT_S:
                inc.note("escalated", "the fix was merged but no production release containing it has served "
                                      "traffic in a week", self.log)
                inc.phase = "escalated"
            return
        current = evidence.window(evs)
        signals = compare(self.watcher.state["baseline"], current)
        live = evs[-1].release
        if not signals:
            # Healthy traffic can still hide wrong answers: the release must also pass the
            # known-answer questions as it is, with no first aid.
            sha = gitops.rev_parse(live, paths.DEPLOY)
            record = canary.run(self.watcher.state, self.log, code_dir=self._release_dir(sha), sha=sha)
            save_state(self.watcher.state)
            ok, how = canary.answers_hold(record, self.watcher.state)
            inc.monitor["canary"] = {"release": sha[:12], "ok": ok, "detail": how}
            if not ok:
                signals = [Signal("quality", "canary_pass_rate", self.watcher.state.get("canary_baseline") or 0.75,
                                  float(record.get("pass_rate") or 0.0))]
        inc.monitor = {**inc.monitor, "window": current, "signals": [s.__dict__ for s in signals]}
        if not signals:
            self._remove_first_aid(inc, "the cure is live")
            inc.note("resolved", f"production healthy on release {live[:8]} (contains the fix): failure rate "
                                 f"{current['failure_rate']:.0%} over {current['n']} traces", self.log)
            inc.phase = "resolved"
            self._tell_neatlogs(inc, f"healed: release {live[:8]} with the fix is healthy in production", resolved=True)
            return
        why = "; ".join(s.describe() for s in signals)
        try:
            revert = delivery.revert_pr(inc, inc.deploy["release"], project.current().branch, paths.DEPLOY, why,
                                        paths.MEDIC_DIR / "worktrees")
            inc.deploy["revert_pr"] = revert
            note = f"still unhealthy with the fix ({why}); revert PR opened: {revert['url']}"
        except delivery.DeliveryError as exc:
            note = f"still unhealthy with the fix ({why}); could not open a revert PR ({exc})"
        inc.note("escalated", note, self.log)
        inc.phase = "escalated"
        self._tell_neatlogs(inc, note)


def _where(exc: BaseException) -> str:
    """Where in Medic an error was raised: the innermost frames of its own code, for the log."""
    import traceback
    ours = ("medic", "medic_runtime", "bisect_agent")
    frames = [f for f in traceback.extract_tb(exc.__traceback__)
              if os.path.basename(os.path.dirname(f.filename)) in ours]
    return " <- ".join(f"{os.path.basename(f.filename)}:{f.lineno} {f.name}" for f in reversed(frames[-3:])) or "?"
