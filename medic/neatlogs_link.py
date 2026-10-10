"""Medic on top of neatlogs: neatlogs finds what broke and why; Medic heals the agent.

neatlogs evaluates every span as it arrives. Its detections are the pattern analysis, its alert
rules decide when a pattern has become an incident, and an alert reaches Medic within seconds
(a webhook rings Medic's doorbell; without a public URL Medic reads the alert history every
tick). Medic opens its incident from the alert, takes neatlogs' investigation as one line of
evidence (over neatlogs' MCP server), and writes what it did back onto the failing traces.

    agent-medic neatlogs setup [--webhook-url https://.../hooks/neatlogs]
    agent-medic neatlogs status

What Medic creates in the project, all named "Medic: ..." so they're easy to find and remove:
detections for failed tool calls, non-answers and refusals; threshold alert rules on them;
metric rules on tool failure rate and tokens; and, given a public URL, one webhook.
"""

import contextlib
import json
import os
import secrets
import time
import uuid
from datetime import datetime
from pathlib import Path

import httpx

from bisect_agent.neatlogs_api import BASE_URL, NeatlogsReader
from medic import paths

MCP_URL = os.environ.get("NEATLOGS_MCP_URL", "https://ingest.neatlogs.com/mcp")
GROUP = "Agent Medic"
PREFIX = "Medic: "

# Kept in step with medic.evidence.NON_ANSWER, so neatlogs flags what Medic counts as failed.
NON_ANSWER_PATTERN = (r"could not|couldn't|unable to|does not (provide|contain|have|include)|"
                      r"no information|not available|no documents|cannot find|can't find")

DETECTIONS = {
    "tool_failed": {
        "displayName": PREFIX + "tool call failed", "type": "conditional",
        "description": "A tool span ended in ERROR: a bad argument, a handler bug or a dead dependency.",
        "runOn": {"frequency": "span_type", "span_types": ["TOOL"]},
        "config": {"root": {"type": "group", "logic": "AND",
                            "children": [{"type": "rule", "field": "status", "operator": "=", "value": "ERROR"}]}}},
    "no_answer": {
        "displayName": PREFIX + "agent gave no answer", "type": "regex",
        "description": "The agent's final answer says it couldn't find or provide what was asked.",
        "runOn": {"frequency": "span_type", "span_types": ["AGENT"]},
        "config": {"output_pattern": NON_ANSWER_PATTERN, "case_sensitive": False, "operator": "OR"}},
    "refusal": {
        "displayName": PREFIX + "agent refused", "type": "classifier",
        "description": "neatlogs' refusal classifier on the agent's final answer.",
        "runOn": {"frequency": "span_type", "span_types": ["AGENT"]},
        "config": {"classifier_slug": "refusals"}},
}

WEBHOOK_EVENTS = ["alert.threshold_breach", "alert.metric_threshold_breach", "alert.per_event_match",
                  "alert.compound_breach", "alert.anomaly_detected"]


def _state_file() -> Path:
    # The neatlogs setup belongs to the project, not to one Medic run: a benchmark run (its own
    # MEDIC_DIR) points here at the project's setup.
    return Path(os.environ.get("MEDIC_NEATLOGS_STATE") or paths.MEDIC_DIR / "neatlogs.json")


def load() -> dict:
    try:
        return json.loads(_state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(state: dict) -> None:
    _state_file().parent.mkdir(parents=True, exist_ok=True)
    _state_file().write_text(json.dumps(state, indent=1), encoding="utf-8")


class Api(NeatlogsReader):
    """The public API, reads and writes, on the reader's shared rate-limit budget."""

    def call(self, method: str, path: str, body: dict | None = None, **params) -> dict:
        if getattr(self, "_client", None) is None:
            self._client = httpx.Client(base_url=BASE_URL, headers=self._headers(), timeout=30)
        # Writes carry an idempotency key, the same one on every retry, so a retried create
        # can never create a second detection, rule or note.
        headers = {"Idempotency-Key": str(uuid.uuid4())} if method != "GET" else {}   # canonical UUIDv4
        for attempt in range(5):
            self._wait_for_budget()
            resp = self._client.request(method, path, json=body, params=params or None, headers=headers)
            self._note_budget(resp)
            if resp.status_code == 429:
                continue
            if resp.status_code >= 400:
                try:
                    problem = resp.json()
                except ValueError:
                    problem = {}
                raise NeatlogsError(resp.status_code, problem.get("code") or problem.get("title") or resp.text[:200])
            return (resp.json() or {}).get("data", {}) if resp.content else {}
        raise NeatlogsError(429, "rate limited")


class NeatlogsError(RuntimeError):
    def __init__(self, status: int, code: str):
        super().__init__(f"neatlogs HTTP {status}: {code}")
        self.status, self.code = status, code


# -- setup --------------------------------------------------------------------------------

def _group(api: Api) -> str:
    groups = api.call("GET", "/detection-groups", limit=50).get("groups", [])
    found = next((g for g in groups if g.get("name") == GROUP), None)
    return found["id"] if found else api.call("POST", "/detection-groups", {"name": GROUP})["id"]


def _alert_rules(detections: dict, baseline: dict | None, webhook_id: str | None) -> dict:
    channels = {"email": False, "slack": False, "webhookIds": [webhook_id] if webhook_id else []}
    rules = {}
    for key, det_id in detections.items():
        rules[key] = {"name": PREFIX + DETECTIONS[key]["displayName"][len(PREFIX):] + " (3 in 5 min)",
                      "ruleType": "threshold", "detectionId": det_id, "thresholdCount": 3, "windowMinutes": 5,
                      "cooldownMinutes": 10, "severity": "critical" if key == "tool_failed" else "warning",
                      "channelMask": channels}
    rules["tool_failure_rate"] = {"name": PREFIX + "tool failure rate above 25%", "ruleType": "metric_threshold",
                                  "metricName": "tool_failure_rate", "metricAggregation": "avg",
                                  "metricOperator": ">", "metricThresholdValue": 0.25,
                                  "windowMinutes": 10, "cooldownMinutes": 10, "severity": "critical",
                                  "channelMask": channels}
    tokens = (baseline or {}).get("tokens_per_run")
    if tokens:
        rules["tokens"] = {"name": PREFIX + "tokens per run 1.5x the healthy baseline", "ruleType": "metric_threshold",
                           "metricName": "tokens_total", "metricAggregation": "avg", "metricOperator": ">",
                           "metricThresholdValue": round(tokens * 1.5), "windowMinutes": 15, "cooldownMinutes": 15,
                           "severity": "warning", "channelMask": channels}
    return rules


def setup(api: Api, baseline: dict | None = None, webhook_url: str | None = None, log=print) -> dict:
    """Create (or find) Medic's detections, alert rules and webhook. Safe to run again."""
    state = load()
    state["group"] = _group(api)
    existing = {d.get("displayName"): d["id"] for d in api.call("GET", "/detections", limit=100).get("detections", [])}
    state.setdefault("detections", {})
    state["skipped"] = {}                       # this run's word on what couldn't be created
    for key, spec in DETECTIONS.items():
        if spec["displayName"] in existing:
            state["detections"][key] = existing[spec["displayName"]]
            continue
        try:
            state["detections"][key] = api.call("POST", "/detections", {**spec, "groupId": state["group"],
                                                                        "isActive": True})["id"]
            log(f"[neatlogs] detection created: {spec['displayName']}")
        except NeatlogsError as exc:            # e.g. a classifier the plan doesn't include
            state["skipped"][key] = str(exc)
            log(f"[neatlogs] detection skipped: {spec['displayName']}: {exc}")
    if webhook_url and not state.get("webhook"):
        state["webhook_secret"] = state.get("webhook_secret") or secrets.token_hex(24)
        state["webhook"] = api.call("POST", "/webhooks", {"name": GROUP, "destinationUrl": webhook_url,
                                                          "eventTypes": WEBHOOK_EVENTS,
                                                          "secret": state["webhook_secret"]})["id"]
        log(f"[neatlogs] webhook created: {webhook_url}")
    existing_rules = {r.get("name"): r["id"] for r in api.call("GET", "/alert-rules", limit=100).get("alertRules", [])}
    state.setdefault("rules", {})
    for key, rule in _alert_rules(state["detections"], baseline, state.get("webhook")).items():
        if rule["name"] in existing_rules:
            state["rules"][key] = existing_rules[rule["name"]]
            continue
        try:
            state["rules"][key] = api.call("POST", "/alert-rules", rule)["id"]
            log(f"[neatlogs] alert rule created: {rule['name']}")
        except NeatlogsError as exc:
            state["skipped"][f"rule:{key}"] = str(exc)
            log(f"[neatlogs] alert rule skipped: {rule['name']}: {exc}")
    state["set_up_at"] = time.time()
    save(state)
    return state


# -- the trigger --------------------------------------------------------------------------

MAX_ALERT_AGE_S = 900    # an alert older than this describes some earlier moment, not now


def new_alerts(api: Api, state: dict | None = None, since: float = 0.0) -> list[dict]:
    """Fires of Medic's own rules that Medic hasn't seen, that fired after `since` (when this
    Medic started watching) and in the last few minutes; oldest first. Older fires are marked
    seen without acting on them: a stale alert must never open an incident."""
    state = state if state is not None else load()
    ours = set((state.get("rules") or {}).values())
    if not ours:
        return []
    seen = set(state.get("seen_alerts") or [])
    fires = api.call("GET", "/alert-history", limit=25).get("alertHistory", [])
    unseen = [f for f in fires if f.get("ruleId") in ours and f["id"] not in seen and not f.get("isFollowup")]
    cutoff = max(since, time.time() - MAX_ALERT_AGE_S)
    fresh = [f for f in unseen if _iso_ts(f.get("firedAt")) >= cutoff]
    if unseen:
        state["seen_alerts"] = sorted(seen | {f["id"] for f in unseen})[-500:]
        save(state)
    return sorted(fresh, key=lambda f: f.get("firedAt", ""))


def doorbell() -> Path:
    """Touched by the webhook receiver; Medic's loop wakes up when it changes."""
    return paths.MEDIC_DIR / "neatlogs" / "doorbell"


def ring() -> None:
    doorbell().parent.mkdir(parents=True, exist_ok=True)
    doorbell().write_text(str(time.time()), encoding="utf-8")


def signal_for(alert: dict) -> dict:
    """An alert as one of Medic's signals."""
    return {"name": "neatlogs_alert", "metric": "neatlogs_alert_hits",
            "baseline": float(alert.get("threshold") or 0), "current": float(alert.get("count") or 0),
            "rule": alert.get("ruleName"), "detection": alert.get("detectionName")}


# -- investigation over MCP ---------------------------------------------------------------

class Mcp:
    """A small Streamable-HTTP MCP client for neatlogs' server (project API key as bearer)."""

    def __init__(self, url: str = MCP_URL, key: str | None = None, timeout: float = 60):
        self.url, self.timeout, self.session, self._id = url, timeout, None, 0
        self.headers = {"Authorization": f"Bearer {key or os.environ.get('NEATLOGS_API_KEY', '')}",
                        "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}

    def _post(self, message: dict) -> dict:
        headers = dict(self.headers)
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        resp = httpx.post(self.url, headers=headers, json=message, timeout=self.timeout)
        if resp.status_code >= 400:
            raise NeatlogsError(resp.status_code, "mcp " + (resp.reason_phrase or ""))
        self.session = resp.headers.get("mcp-session-id") or self.session
        text = resp.text
        if "text/event-stream" in resp.headers.get("content-type", ""):
            text = next((line[5:].strip() for line in text.splitlines() if line.startswith("data:")), "")
        return json.loads(text) if text else {}

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        reply = self._post({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}})
        if "error" in reply:
            raise NeatlogsError(400, f"mcp {method}: {reply['error'].get('message')}")
        return reply.get("result") or {}

    def connect(self) -> "Mcp":
        self._rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                 "clientInfo": {"name": "agent-medic", "version": "0.1"}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self

    def tool(self, name: str, **arguments):
        """Call a tool; returns its JSON content when it has some, else its text."""
        result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        texts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
        joined = "\n".join(texts)
        try:
            return json.loads(joined)
        except ValueError:
            return joined


def _cards(listing) -> list[dict]:
    if isinstance(listing, dict):
        listing = listing.get("triage_cards") or listing.get("cards") or []
    return [c for c in listing or [] if isinstance(c, dict)]


def _linked(card: dict) -> set[str]:
    linked = card.get("linked_trace_ids") or []
    if isinstance(linked, str):                 # some responses carry the list as a string
        linked = [t.strip(" '\"") for t in linked.strip("[]").split(",")]
    return {t for t in linked if t}


def _iso_ts(value) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


RECENT_CARD_S = 3600      # a card for the same workflow this close to the incident is about it


def investigate(trace_ids: list[str], opened_at: float, workflow: str = paths.WORKFLOW,
                mcp: Mcp | None = None) -> dict:
    """neatlogs' own investigation of this failure (its fix card), as an investigator finding."""
    finding = {"investigator": "neatlogs", "verdict": "none", "confidence": 0.0, "detail": ""}
    try:
        mcp = mcp or Mcp().connect()
        cards = _cards(mcp.tool("triage_list"))
    except Exception as exc:  # noqa: BLE001 - neatlogs being unreachable must not stop the diagnosis
        return {**finding, "verdict": "unavailable", "detail": f"neatlogs MCP: {type(exc).__name__}: {exc}"}
    ids = set(trace_ids)
    # The card that links these very traces; else a fresh card for the same workflow. Never an
    # unrelated card: that would be evidence about some other failure.
    matched = sorted(cards, key=lambda c: -len(_linked(c) & ids))
    card = next((c for c in matched if _linked(c) & ids), None)
    if card is None:
        fresh = [c for c in cards if c.get("workflow_name") == workflow
                 and _iso_ts(c.get("created_at")) >= opened_at - RECENT_CARD_S]
        card = max(fresh, key=lambda c: _iso_ts(c.get("created_at")), default=None)
    if card is None:
        return {**finding, "detail": "neatlogs has no fix card for these traces yet"}
    detail, trail = card, None
    with contextlib.suppress(Exception):
        detail = mcp.tool("triage_get", triage_id=card["triage_id"]) or card
    if card.get("thread_id"):
        with contextlib.suppress(Exception):
            trail = mcp.tool("triage_get_investigation", thread_id=card["thread_id"])
    d = {**card, **(detail if isinstance(detail, dict) else {})}
    try:
        confidence = float(d.get("confidence") or 0.6)
    except (TypeError, ValueError):
        confidence = 0.6
    return {"investigator": "neatlogs", "verdict": "root_cause", "suggestion_id": card["triage_id"],
            "confidence": round(confidence, 2), "detail": str(d.get("title") or "")[:300],
            "root_cause": str(d.get("root_cause") or "")[:800], "label": d.get("triage_label"),
            "severity": d.get("severity"), "location": d.get("location") or d.get("file"),
            "analysis": str(d.get("analysis") or d.get("root_cause") or "")[:800],
            "matched_traces": len(_linked(card) & ids),
            "investigation": trail if isinstance(trail, (dict, list)) else None}


# -- writing back -------------------------------------------------------------------------

# Trace feedback and comments are for people (neatlogs accepts them only from a user's OAuth
# login). Medic writes where automation belongs: its own work is traced into neatlogs (one
# session per incident), and its proven findings go onto neatlogs' fix card as new versions.
# Card edits are recorded under a person's team email, which neatlogs asks the user to agree
# to: Medic writes them only when the user has set NEATLOGS_USER_EMAIL.

def _email() -> str | None:
    return os.environ.get("NEATLOGS_USER_EMAIL") or None


def update_card(triage_id: str | None, mcp: Mcp | None = None, **sections) -> bool:
    """Add Medic's proven findings to neatlogs' fix card: root_cause, evidence_reasoning,
    recommended_action_md (markdown; each edit appends a version) or resolution_note."""
    email = _email()
    fields = {k: v for k, v in sections.items() if v}
    if not triage_id or not email or not fields:
        return False
    try:
        (mcp or Mcp().connect()).tool("triage_update", triage_id=triage_id, user_email=email, **fields)
        return True
    except Exception:  # noqa: BLE001
        return False


def update_fix(suggestion_id: str | None, status: str, note: str = "", mcp: Mcp | None = None) -> bool:
    """Move neatlogs' fix card on its board. `resolved` (the fix shipped) also opens neatlogs'
    own verification watch; resolved and archived need a resolution note."""
    email = _email()
    if not suggestion_id or not email:
        return False
    try:
        (mcp or Mcp().connect()).tool("triage_update_status", triage_id=suggestion_id, user_email=email, status=status,
                                      **({"resolution_note": note[:1000]} if note else {}))
        return True
    except Exception:  # noqa: BLE001
        return False
