"""First-aid bundles: what Medic publishes and the runtime applies.

    {"payload": {...}, "sig": "<hex hmac-sha256 of the canonical payload>"}

payload:
    incident      the incident it treats
    release       the release it applies to (12-char sha); any other release ignores it
    issued_at     epoch seconds
    expires_at    epoch seconds; an expired bundle is ignored (first aid never becomes permanent)
    functions     {name: {"source": "def name(...): ...", "from_release": sha}}
    values        {name: value}
    breakers      {tool name: "open"}
    fallback_models [model, ...]
"""

import hashlib
import hmac
import json

FIELDS = ("functions", "values", "breakers", "fallback_models")


def canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign(payload: dict, key: str | None) -> dict:
    doc = {"payload": payload}
    if key:
        doc["sig"] = hmac.new(key.encode("utf-8"), canonical(payload), hashlib.sha256).hexdigest()
    return doc


def verify(doc: dict, key: str | None) -> bool:
    """With a key, the signature must match. Without one, only unsigned local bundles are
    accepted (the caller decides what counts as local)."""
    if not isinstance(doc, dict) or not isinstance(doc.get("payload"), dict):
        return False
    if not key:
        return "sig" not in doc
    expected = hmac.new(key.encode("utf-8"), canonical(doc["payload"]), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, str(doc.get("sig", "")))


def empty(payload: dict) -> bool:
    return not any(payload.get(f) for f in FIELDS)
