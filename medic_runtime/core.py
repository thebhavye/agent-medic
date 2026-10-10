"""The runtime's guards and the first-aid bundle they read.

Environment:
    MEDIC_CONTROL          path or http(s) URL of the first-aid bundle (unset: no first aid)
    MEDIC_RELEASE          this process's release sha; a bundle applies only to its own release
    MEDIC_RUNTIME_KEY      shared secret that signs bundles; required for URLs, and when set every
                           bundle must be signed. It is never sent anywhere.
    MEDIC_CONTROL_TOKEN    optional bearer token for fetching a remote bundle (HTTPS, or HTTP to loopback)
    MEDIC_FALLBACK_MODELS  comma-separated models to try when the configured one is unavailable
"""

import ast
import contextlib
import contextvars
import functools
import hashlib
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request

from medic_runtime import bundle as _bundle

LOOPBACK = {"127.0.0.1", "localhost", "::1"}

CACHE_S = 5.0             # how often the bundle is re-read
LOOP_LIMIT = 2            # identical tool calls allowed per request before the guard steps in
BREAKER_FAILURES = 3      # consecutive failures that open a tool's local breaker
BREAKER_COOLDOWN_S = 30.0

# Errors that mean "this model can't serve you", not "your request was bad".
MODEL_UNAVAILABLE = re.compile(
    r"\b404\b|not[ _]found|does not exist|retired|deprecated|decommissioned|no longer (available|supported)|"
    r"\b429\b|quota|rate.?limit|resource_exhausted|overloaded|\b503\b", re.I)


class FirstAidError(RuntimeError):
    """Raised instead of calling a tool the runtime has cut off; the agent reports it to the model."""


_lock = threading.Lock()
_cache = {"at": 0.0, "source": None, "payload": None}
_surfaces: dict = {}               # name -> original function
_values: set = set()
_compiled: dict = {}               # (name, source hash) -> patched function
_breakers: dict = {}               # tool -> {"failures": n, "open_until": t}
_log: list = []
_request = contextvars.ContextVar("medic_request", default=None)


# -- the bundle ---------------------------------------------------------------------------

def _remote_allowed(source: str) -> bool:
    """HTTPS anywhere; plain HTTP only to this machine."""
    parsed = urllib.parse.urlparse(source)
    return parsed.scheme == "https" or (parsed.scheme == "http" and parsed.hostname in LOOPBACK)


def _read(source: str, key: str | None) -> dict | None:
    if source.startswith(("http://", "https://")):
        if not key or not _remote_allowed(source):
            return None                 # a remote bundle must be signed, and fetched over TLS
        # The signing key never leaves this process; fetching uses its own optional token.
        token = os.environ.get("MEDIC_CONTROL_TOKEN")
        req = urllib.request.Request(source, headers={"Authorization": f"Bearer {token}"} if token else {})
        with urllib.request.urlopen(req, timeout=3) as resp:
            doc = json.loads(resp.read().decode("utf-8"))
    else:
        with open(source, encoding="utf-8") as f:
            doc = json.load(f)
    return doc["payload"] if _bundle.verify(doc, key) else None


def _payload() -> dict | None:
    """The bundle that applies to this process right now, or None."""
    source = os.environ.get("MEDIC_CONTROL")
    if not source:
        return None
    now = time.time()
    with _lock:
        if _cache["source"] == source and now - _cache["at"] < CACHE_S:
            payload = _cache["payload"]
        else:
            try:
                payload = _read(source, os.environ.get("MEDIC_RUNTIME_KEY") or None)
            except (OSError, ValueError, KeyError):
                payload = None          # no bundle, unreadable or tampered: run unaided
            _cache.update(at=now, source=source, payload=payload)
    if not payload:
        return None
    release = (os.environ.get("MEDIC_RELEASE") or "")[:12]
    if not release or str(payload.get("release", ""))[:12] != release:
        return None                     # first aid treats one release; a new deploy outgrows it
    if float(payload.get("expires_at") or 0) < now:
        return None
    return payload


def reset() -> None:
    """Forget cached state (tests, or after changing the environment)."""
    with _lock:
        _cache.update(at=0.0, source=None, payload=None)
        _compiled.clear()
        _breakers.clear()
        _log.clear()


def status() -> dict:
    payload = _payload()
    return {"active": bool(payload and not _bundle.empty(payload)), "incident": (payload or {}).get("incident"),
            "surfaces": sorted(_surfaces), "values": sorted(_values),
            "patched": sorted(((payload or {}).get("functions") or {}).keys()),
            "pinned": sorted(((payload or {}).get("values") or {}).keys())}


# -- reporting ----------------------------------------------------------------------------

def _report(kind: str, name: str, detail: str) -> None:
    """Record an intervention, once per request, as a neatlogs span Medic can read back."""
    scope = _request.get()
    key = (kind, name)
    if scope is not None:
        if key in scope["reported"]:
            return
        scope["reported"].add(key)
    entry = {"at": time.time(), "kind": kind, "name": name, "detail": detail}
    with _lock:
        _log.append(entry)
        del _log[:-200]
    try:
        import neatlogs
        neatlogs.span(kind="CHAIN", name=f"medic:first_aid:{kind}:{name}")(lambda: entry)()
    except Exception:  # noqa: BLE001 - reporting must never break the agent
        pass


def interventions() -> list[dict]:
    with _lock:
        return list(_log)


# -- surfaces and values ------------------------------------------------------------------

def _compile(name: str, source: str, original):
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    fn = _compiled.get((name, digest))
    if fn is not None:
        return fn
    tree = ast.parse(source)
    defs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if len(tree.body) != 1 or len(defs) != 1 or defs[0].name != original.__name__:
        raise ValueError(f"first-aid source for {name} must be exactly one def {original.__name__}()")
    defs[0].decorator_list = []        # the healthy version runs bare, outside this wrapper
    namespace = dict(original.__globals__)
    exec(compile(tree, f"<medic first aid: {name}>", "exec"), namespace)  # noqa: S102 - signed, registered names only
    fn = namespace[original.__name__]
    _compiled[(name, digest)] = fn
    return fn


def surface(name: str):
    """Register a function Medic may replace with its known-good version during an incident."""
    def wrap(fn):
        _surfaces[name] = fn

        @functools.wraps(fn)
        def call(*args, **kwargs):
            patch = ((_payload() or {}).get("functions") or {}).get(name)
            if patch:
                try:
                    healthy = _compile(name, patch["source"], fn)
                except Exception:  # noqa: BLE001 - incl. NameError while the def runs (a default or
                    healthy = None  # annotation the culprit removed): a bad patch runs the original
                if healthy is not None:
                    _report("function", name, f"running {name} from release {str(patch.get('from_release', ''))[:8]}")
                    return healthy(*args, **kwargs)
            return fn(*args, **kwargs)
        call.__medic_surface__ = name
        return call
    return wrap


def value(name: str, default):
    """A tunable read at call time (a prompt, a schema, a parameter); Medic may pin a known-good one."""
    _values.add(name)
    pinned = (_payload() or {}).get("values") or {}
    if name in pinned:
        _report("value", name, f"{name} pinned to its last healthy value")
        return pinned[name]
    return default


# -- request-scoped guards ----------------------------------------------------------------

@contextlib.contextmanager
def request():
    """One agent request: the scope for the loop guard and for reporting each fix once."""
    token = _request.set({"calls": {}, "reported": set()})
    try:
        yield
    finally:
        _request.reset(token)


def guard_tool(name: str, args, run):
    """Call a tool through the breakers and the loop guard."""
    if ((_payload() or {}).get("breakers") or {}).get(name) == "open":
        _report("breaker", name, f"{name} is cut off by Medic while its dependency is down")
        raise FirstAidError(f"{name} is temporarily unavailable; answer without it and say so")
    state = _breakers.setdefault(name, {"failures": 0, "open_until": 0.0})
    if state["open_until"] > time.time():
        _report("breaker", name, f"{name} failed {BREAKER_FAILURES} times in a row; paused")
        raise FirstAidError(f"{name} is temporarily unavailable; answer without it and say so")

    scope = _request.get()
    if scope is not None:
        key = (name, json.dumps(args, sort_keys=True, default=str))
        seen = scope["calls"].get(key, 0)
        scope["calls"][key] = seen + 1
        if seen >= LOOP_LIMIT:
            _report("loop", name, f"{name} called {seen + 1} times with the same arguments")
            return (f"You already called {name} with these exact arguments {seen} times and have the result. "
                    "Do not call it again: answer from what you already know.")
    try:
        result = run()
    except FirstAidError:
        raise
    except Exception as exc:
        # Only "the dependency is down" counts toward the shared breaker. A bad argument or a
        # missing document is that call's problem; it must not cut the tool off for everyone.
        if dependency_down(exc):
            state["failures"] += 1
            if state["failures"] >= BREAKER_FAILURES:
                state["open_until"] = time.time() + BREAKER_COOLDOWN_S
        raise
    state["failures"] = 0
    return result


DOWN_TYPES = {"ConnectError", "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout", "TimeoutException",
              "RemoteProtocolError", "ServiceUnavailable", "APIConnectionError", "APITimeoutError"}


def dependency_down(exc: BaseException) -> bool:
    """Connection failures, timeouts and 5xx responses: the tool's backend can't serve anyone."""
    if isinstance(exc, (ConnectionError, TimeoutError)) or type(exc).__name__ in DOWN_TYPES:
        return True
    status = getattr(getattr(exc, "response", None), "status_code", None) or getattr(exc, "status_code", None)
    return isinstance(status, int) and status >= 500


def call_model(fn, model: str):
    """Call fn(model); if that model can't serve (retired, quota, overloaded), try the fallbacks."""
    fallbacks = list((_payload() or {}).get("fallback_models") or [])
    fallbacks += [m.strip() for m in os.environ.get("MEDIC_FALLBACK_MODELS", "").split(",") if m.strip()]
    candidates = [model] + [m for i, m in enumerate(fallbacks) if m != model and m not in fallbacks[:i]]
    for i, candidate in enumerate(candidates):
        try:
            result = fn(candidate)
        except Exception as exc:
            if i + 1 < len(candidates) and MODEL_UNAVAILABLE.search(f"{type(exc).__name__}: {exc}"):
                _report("fallback", candidate, f"{candidate} unavailable ({type(exc).__name__}); "
                                               f"trying {candidates[i + 1]}")
                continue
            raise
        return result
    raise RuntimeError("no model candidates")   # unreachable: the loop returns or raises
