"""The runtime's guards and the first-aid bundle they read.

Environment:
    MEDIC_CONTROL          path or http(s) URL of the first-aid bundle (unset: no first aid)
    MEDIC_RELEASE          this process's release sha; a bundle applies only to its own release
                           (default: GIT_SHA, which also tags the process's neatlogs traces)
    MEDIC_RUNTIME_KEY      shared secret that signs bundles; required for URLs, and when set every
                           bundle must be signed. It is never sent anywhere.
    MEDIC_CONTROL_TOKEN    optional bearer token for fetching a remote bundle (HTTPS, or HTTP to loopback)
    MEDIC_FALLBACK_MODELS  comma-separated models to try when the configured one is unavailable
    MEDIC_STANDBY          path or URL of the standby bundle: the last known good version of every
                           registered part, which the runtime engages by itself (armed mode)
    MEDIC_RUNTIME_STATE    optional file shared by this machine's processes, so one engagement
                           protects every worker of the release
"""

import ast
import contextlib
import contextvars
import functools
import hashlib
import inspect
import json
import os
import re
import secrets
import textwrap
import threading
import time
import urllib.error
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


# Armed mode: the runtime holds the last known good version of every registered part (the
# standby bundle) and switches the parts that changed to it by itself when requests start
# failing in a way code causes. No round trip to Medic: healing takes a few failed requests.
TRIP_WINDOW = 6           # recent requests the runtime judges
TRIP_FAILURES = 3         # code-shaped failures among them that engage the standby
ENGAGE_TTL_S = 2 * 3600   # an engagement this old is dropped: Medic should have taken over
NOT_CODE_COOLDOWN_S = 600  # after a standby didn't help, don't try again for this long

NON_ANSWER = re.compile(r"could not|couldn't|unable to|does not (provide|contain|have|include)|"
                        r"no information|not available|no documents|cannot find|can't find", re.I)


class FirstAidError(RuntimeError):
    """Raised instead of calling a tool the runtime has cut off; the agent reports it to the model."""


_lock = threading.Lock()
_state_lock = threading.Lock()     # serializes engagement decisions within this process
_cache: dict = {}                  # env var -> {"at", "source", "payload"}
_surfaces: dict = {}               # name -> original function
_values: set = set()
_defaults: dict = {}               # value name -> the value the live code passes
_compiled: dict = {}               # (name, source hash) -> patched function
_breakers: dict = {}               # tool -> {"failures": n, "open_until": t}
_log: list = []
_outcomes: list = []               # [(failed, engaged)] for recent requests
_local_state: dict = {}            # engagement when no shared state file is configured
_request = contextvars.ContextVar("medic_request", default=None)
_last = threading.local()          # the last finished request's stats, per thread (for runners)


def last_request() -> dict:
    """{"tool_calls", "failed", "outage"} of the last request this thread finished."""
    return dict(getattr(_last, "stats", {}) or {})


# -- the bundle ---------------------------------------------------------------------------

class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirects are refused for first-aid bundles", headers, fp)


_NO_REDIRECTS = urllib.request.build_opener(_RefuseRedirect)


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
        # No redirects: urllib would forward the token to wherever the redirect points.
        with _NO_REDIRECTS.open(req, timeout=3) as resp:
            doc = json.loads(resp.read().decode("utf-8"))
    else:
        with open(source, encoding="utf-8") as f:
            doc = json.load(f)
    return doc["payload"] if _bundle.verify(doc, key) else None


def _load(env: str) -> dict | None:
    """The verified payload of the bundle named by an environment variable (cached a few seconds)."""
    source = os.environ.get(env)
    if not source:
        return None
    now = time.time()
    with _lock:
        entry = _cache.get(env)
        if entry and entry["source"] == source and now - entry["at"] < CACHE_S:
            return entry["payload"]
    try:
        payload = _read(source, os.environ.get("MEDIC_RUNTIME_KEY") or None)
    except (OSError, ValueError, KeyError):
        payload = None                  # no bundle, unreadable or tampered: run unaided
    with _lock:
        _cache[env] = {"at": now, "source": source, "payload": payload}
    return payload


def _release() -> str:
    return (os.environ.get("MEDIC_RELEASE") or os.environ.get("GIT_SHA") or "")[:12]


def _payload() -> dict | None:
    """Medic's targeted first aid for this release, or None."""
    payload = _load("MEDIC_CONTROL")
    if not payload:
        return None
    if not _release() or str(payload.get("release", ""))[:12] != _release():
        return None                     # first aid treats one release; a new deploy outgrows it
    if float(payload.get("expires_at") or 0) < time.time():
        return None
    return payload


def _standby() -> dict | None:
    """The last known good versions, held for any release other than the good one."""
    payload = _load("MEDIC_STANDBY")
    if not payload or not _release() or str(payload.get("good_release", ""))[:12] == _release():
        return None
    if float(payload.get("expires_at") or 0) < time.time():
        return None
    return payload


def reset() -> None:
    """Forget cached state (tests, or after changing the environment)."""
    with _lock:
        _cache.clear()
        _compiled.clear()
        _breakers.clear()
        _log.clear()
        _outcomes.clear()
        _local_state.clear()
        _defaults.clear()


# -- armed mode: engagement, shared by every process of this release on the machine -----

def _state_path() -> str | None:
    return os.environ.get("MEDIC_RUNTIME_STATE") or None


def _read_state() -> dict:
    path = _state_path()
    if not path:
        return dict(_local_state)
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write_state(state: dict) -> None:
    path = _state_path()
    if not path:
        _local_state.clear()
        _local_state.update(state)
        return
    # A temporary file per write (process, thread, random), published with an atomic replace:
    # a reader sees the old state or the new one, never a half-written file.
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(4)}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.remove(tmp)              # another process won the race; its write is as good


def _engagement() -> dict | None:
    """This release's live engagement: {"names", "good_release", "at"}, or None."""
    entry = _read_state().get(_release())
    standby = _standby()
    if not entry or entry.get("status") != "engaged" or not standby:
        return None
    if entry.get("good_release") != str(standby.get("good_release", ""))[:12]:
        return None                     # the standby moved on: that engagement is stale
    if time.time() - float(entry.get("at") or 0) > ENGAGE_TTL_S:
        return None
    return entry


def _normalized(source: str) -> str | None:
    try:
        tree = ast.parse(textwrap.dedent(source))
    except SyntaxError:
        return None
    defs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if len(defs) != 1:
        return None
    defs[0].decorator_list = []
    return ast.dump(defs[0])


def _changed_since_good(standby: dict) -> list[str]:
    """Registered parts whose live version differs from the last known good one."""
    changed = []
    for name, patch in (standby.get("functions") or {}).items():
        original = _surfaces.get(name)
        if original is None:
            continue
        try:
            live = _normalized(inspect.getsource(original))
        except (OSError, TypeError):
            continue
        if live is not None and live != _normalized(patch.get("source", "")):
            changed.append(name)
    for name, good in (standby.get("values") or {}).items():
        if name in _defaults and _defaults[name] != good:
            changed.append(name)
    return sorted(changed)


def _record_outcome(failed: bool, started_engaged: bool = False) -> None:
    """Judge the last few requests; engage the standby, or give it up if it didn't help.

    A request counts for the side it started on, so one that fails before it reaches a
    replaced part still counts against the standby. A request that crossed the switch (started
    on one side, finished on the other; requests run in parallel) is evidence for neither."""
    standby = _standby()
    if standby is None:
        return
    if _payload() is not None:
        return                          # Medic's targeted first aid is in charge of this release
    # One decision at a time in this process: requests run in parallel threads, and two of them
    # deciding together would each rewrite the state from the same stale read.
    with _state_lock:
        engaged = _engagement()
        if bool(engaged) != started_engaged:
            return                      # crossed the switch
        with _lock:
            _outcomes.append((failed, started_engaged))
            del _outcomes[:-TRIP_WINDOW * 4]
            window = [f for f, a in _outcomes if a == bool(engaged)][-TRIP_WINDOW:]
        state = _read_state()
        entry = state.get(_release()) or {}
        report = None
        if engaged is None:
            if entry.get("status") == "not_code" and time.time() - float(entry.get("at") or 0) < NOT_CODE_COOLDOWN_S:
                return
            if sum(window) < TRIP_FAILURES:
                return
            names = _changed_since_good(standby)
            if not names:
                return                  # nothing changed since the good release: not a code problem
            state[_release()] = {"status": "engaged", "names": names, "at": time.time(),
                                 "good_release": str(standby.get("good_release", ""))[:12]}
            report = ("engaged", f"{sum(window)} of the last {len(window)} requests failed; running "
                                 f"{', '.join(names)} from {str(standby.get('good_release', ''))[:8]}")
        elif len(window) >= TRIP_WINDOW and sum(window) >= TRIP_FAILURES:
            # Still failing with the last known good in place: the cause isn't these parts.
            state[_release()] = {"status": "not_code", "at": time.time()}
            report = ("released", "the last known good versions did not help; back to the live code")
        if report is None:
            return
        _write_state(state)
        with _lock:
            _outcomes.clear()
    _report("auto", *report)


def _patch(kind: str, name: str):
    """(how, patch) for a registered part.

    A targeted bundle for this release replaces the automatic engagement entirely: production
    then runs exactly the combination Medic proved in its trial, not that plus leftovers."""
    payload = _payload()
    if payload is not None:
        targeted = payload.get(kind) or {}
        if name in targeted:
            return ("function" if kind == "functions" else "value"), targeted[name]
        return None, None
    engaged = _engagement()
    if engaged and name in engaged.get("names", []):
        standby = (_standby() or {}).get(kind) or {}
        if name in standby:
            return "auto", standby[name]
    return None, None


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
            how, patch = _patch("functions", name)
            if patch:
                try:
                    healthy = _compile(name, patch["source"], fn)
                except Exception:  # noqa: BLE001 - incl. NameError while the def runs (a default or
                    healthy = None  # annotation the culprit removed): a bad patch runs the original
                if healthy is not None:
                    _report(how, name, f"running {name} from release {str(patch.get('from_release', ''))[:8]}")
                    return healthy(*args, **kwargs)
            return fn(*args, **kwargs)
        call.__medic_surface__ = name
        return call
    return wrap


def value(name: str, default):
    """A tunable read at call time (a prompt, a schema, a parameter); Medic may pin a known-good one."""
    _values.add(name)
    _defaults[name] = default
    how, pinned = _patch("values", name)
    if how:
        _report(how, name, f"{name} pinned to its last healthy value")
        return pinned
    return default


# -- request-scoped guards ----------------------------------------------------------------

@contextlib.contextmanager
def request():
    """One agent request: the scope for the loop guard, for reporting each fix once, and the
    unit armed mode judges (did this request fail the way code makes requests fail?)."""
    scope = {"calls": {}, "reported": set(), "failed": False, "outage": False, "tool_calls": 0,
             "engaged": _engagement() is not None if _standby() else False}
    token = _request.set(scope)
    try:
        yield
    except Exception as exc:
        # Only real dependency failures here. Model-error text ("404", "not found") is matched
        # inside call_model(), where it can only mean the model; at this boundary the same words
        # may be a missing document that changed code asked for, which is a code failure.
        if dependency_down(exc):
            scope["outage"] = True
        else:
            scope["failed"] = True
        raise
    finally:
        _request.reset(token)
        _last.stats = {"tool_calls": scope["tool_calls"], "failed": scope["failed"], "outage": scope["outage"]}
        try:
            # A request hit by an outage (a dead tool backend, an unavailable model) says nothing
            # about the code: it counts neither for nor against the standby.
            if not scope["outage"]:
                # A request that blew far past the healthy tool-call budget failed: a cost regression.
                budget = ((_standby() or {}).get("limits") or {}).get("tool_calls_per_request")
                if budget and scope["tool_calls"] > budget:
                    scope["failed"] = True
                _record_outcome(scope["failed"], scope["engaged"])
        except Exception:  # noqa: BLE001 - judging must never break the agent
            pass


def entrypoint(fn):
    """Mark the function that answers one request (`answer(question) -> str` or a dict with
    "answer"). Each call becomes one request for the guards and armed mode, and one AGENT span
    in neatlogs carrying the question and the answer, which is what Medic reads back."""
    is_async = inspect.iscoroutinefunction(fn)
    span = None
    try:
        import neatlogs

        if is_async:
            async def _agent(question: str, _run):
                result = await _run()
                return {"answer": _answer_text(result), "_result": result}
        else:
            def _agent(question: str, _run):
                result = _run()
                return {"answer": _answer_text(result), "_result": result}
        span = neatlogs.span(kind="AGENT", name=fn.__name__)(_agent)
    except Exception:  # noqa: BLE001 - without neatlogs the guards still work
        span = None

    def _question(args, kwargs):
        return str(args[0] if args else next(iter(kwargs.values()), ""))

    if is_async:                         # the request stays open until the answer exists
        @functools.wraps(fn)
        async def call(*args, **kwargs):
            with request():
                if span is not None:
                    out = await span(_question(args, kwargs), lambda: fn(*args, **kwargs))
                    result, text = out["_result"], out["answer"]
                else:
                    result = await fn(*args, **kwargs)
                    text = _answer_text(result)
                answer(text or "")
                return result
    else:
        @functools.wraps(fn)
        def call(*args, **kwargs):
            with request():
                if span is not None:
                    out = span(_question(args, kwargs), lambda: fn(*args, **kwargs))
                    result, text = out["_result"], out["answer"]
                else:
                    result = fn(*args, **kwargs)
                    text = _answer_text(result)
                answer(text or "")
                return result
    call.__medic_entrypoint__ = True
    return call


def _answer_text(result) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        return str(result.get("answer") or "")
    return str(result or "")


def answer(text: str) -> str:
    """Tell the runtime what the agent answered: a non-answer counts as a failed request."""
    scope = _request.get()
    if scope is not None and (not text or NON_ANSWER.search(text)):
        scope["failed"] = True
    return text


def failed(reason: str = "") -> None:
    """Tell the runtime this request ended without an answer (an error, a refusal, max tokens)."""
    scope = _request.get()
    if scope is not None:
        scope["failed"] = True


def guard_tool(name: str, args, run):
    """Call a tool through the breakers and the loop guard."""
    scope = _request.get()
    cut_off = ((_payload() or {}).get("breakers") or {}).get(name) == "open"
    state = _breakers.setdefault(name, {"failures": 0, "open_until": 0.0})
    if cut_off or state["open_until"] > time.time():
        _report("breaker", name, f"{name} is cut off by Medic while its dependency is down" if cut_off
                else f"{name} failed {BREAKER_FAILURES} times in a row; paused")
        if scope is not None:
            scope["outage"] = True      # a dependency outage, not the code: armed mode ignores it
        raise FirstAidError(f"{name} is temporarily unavailable; answer without it and say so")

    if scope is not None:
        scope["tool_calls"] += 1
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
            if scope is not None:
                scope["outage"] = True
        elif scope is not None:
            scope["failed"] = True      # code-shaped (bad args, a handler bug): what armed mode heals
        raise
    state["failures"] = 0
    return result


DOWN_TYPES = {"ConnectError", "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout", "TimeoutException",
              "RemoteProtocolError", "ServiceUnavailable", "APIConnectionError", "APITimeoutError"}


def model_unavailable(exc: BaseException) -> bool:
    return bool(MODEL_UNAVAILABLE.search(f"{type(exc).__name__}: {exc}"))


def outage(exc: BaseException) -> bool:
    """Something outside the agent's code can't serve right now: a tool backend or the model."""
    return dependency_down(exc) or model_unavailable(exc)


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
            if i + 1 < len(candidates) and model_unavailable(exc):
                _report("fallback", candidate, f"{candidate} unavailable ({type(exc).__name__}); "
                                               f"trying {candidates[i + 1]}")
                continue
            scope = _request.get()
            if scope is not None and outage(exc):
                scope["outage"] = True  # even if the agent catches it and finishes with an error
            raise
        return result
    raise RuntimeError("no model candidates")   # unreachable: the loop returns or raises
