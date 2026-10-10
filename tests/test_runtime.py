"""The runtime that ships inside the user's agent: first aid applies only when it should."""

import json
import time

import pytest

import medic_runtime as medic
from medic_runtime import bundle, core


@pytest.fixture
def control(tmp_path, monkeypatch):
    path = tmp_path / "first_aid.json"
    monkeypatch.setenv("MEDIC_CONTROL", str(path))
    monkeypatch.setenv("MEDIC_RELEASE", "b" * 12)
    monkeypatch.delenv("MEDIC_RUNTIME_KEY", raising=False)
    monkeypatch.delenv("MEDIC_FALLBACK_MODELS", raising=False)
    medic.reset()

    def publish(key=None, **fields):
        body = {"incident": "inc1", "release": "b" * 12, "issued_at": time.time(),
                "expires_at": time.time() + 600, **fields}
        path.write_text(json.dumps(bundle.sign(body, key)), encoding="utf-8")
        medic.reset()
    yield publish
    medic.reset()


@medic.surface("cap")
def capped(text: str) -> str:
    return text[:3]


def test_no_bundle_runs_the_code_as_written(control):
    assert capped("abcdef") == "abc" and medic.value("prompt", "v1") == "v1"
    assert medic.interventions() == []


def test_function_runs_its_healthy_version(control):
    control(functions={"cap": {"source": "def capped(text: str) -> str:\n    return text\n", "from_release": "a" * 40}})
    assert capped("abcdef") == "abcdef"
    assert medic.interventions()[-1]["kind"] == "function"


def test_value_is_pinned(control):
    control(values={"prompt": "healthy prompt"})
    assert medic.value("prompt", "broken prompt") == "healthy prompt"


@pytest.mark.parametrize("change", [
    {"release": "c" * 12},                      # issued for another release: a new deploy outgrows it
    {"expires_at": time.time() - 1},            # expired: first aid never becomes permanent
])
def test_bundle_applies_only_to_its_release_and_until_it_expires(control, change):
    control(values={"prompt": "healthy"}, **change)
    assert medic.value("prompt", "live") == "live"


def test_signed_bundles_need_the_right_key(control, monkeypatch):
    monkeypatch.setenv("MEDIC_RUNTIME_KEY", "secret")
    control(key="wrong", values={"prompt": "tampered"})
    assert medic.value("prompt", "live") == "live"
    control(key="secret", values={"prompt": "healthy"})
    assert medic.value("prompt", "live") == "healthy"
    control(key=None, values={"prompt": "unsigned"})          # a key is set, so unsigned is refused
    assert medic.value("prompt", "live") == "live"


def test_remote_bundle_without_a_key_is_ignored(monkeypatch):
    monkeypatch.setenv("MEDIC_CONTROL", "http://127.0.0.1:9/never-fetched")
    monkeypatch.setenv("MEDIC_RELEASE", "b" * 12)
    monkeypatch.delenv("MEDIC_RUNTIME_KEY", raising=False)
    medic.reset()
    assert medic.value("prompt", "live") == "live"


@pytest.mark.parametrize("source", [
    "def other(text):\n    return text\n",                          # wrong name
    "import os\ndef capped(text):\n    return text\n",              # more than one statement
    "def capped(text:\n",                                           # does not parse
])
def test_bad_patches_never_take_the_agent_down(control, source):
    control(functions={"cap": {"source": source, "from_release": "a" * 40}})
    assert capped("abcdef") == "abc"


def test_unregistered_names_cannot_be_patched(control):
    control(functions={"not_registered": {"source": "def capped(t):\n    return 'x'\n"}})
    assert capped("abcdef") == "abc"


def test_loop_guard_stops_identical_calls_within_a_request(control):
    calls = []
    with medic.request():
        for _ in range(4):
            out = medic.guard_tool("search", {"q": "x"}, lambda: calls.append(1) or "result")
    assert len(calls) == core.LOOP_LIMIT and "already called search" in out
    with medic.request():                                   # a new request starts fresh
        assert medic.guard_tool("search", {"q": "x"}, lambda: "result") == "result"


def test_breakers(control):
    control(breakers={"kb": "open"})
    with pytest.raises(medic.FirstAidError):
        medic.guard_tool("kb", {}, lambda: "never")
    control()                                               # Medic's breaker lifted

    def boom():
        raise ConnectionError("down")
    for _ in range(core.BREAKER_FAILURES):
        with pytest.raises(ConnectionError):
            medic.guard_tool("api", {}, boom)
    with pytest.raises(medic.FirstAidError):                # the local breaker opened
        medic.guard_tool("api", {}, lambda: "ok")


def test_model_fallback_only_for_unavailable_models(control, monkeypatch):
    monkeypatch.setenv("MEDIC_FALLBACK_MODELS", "model-b")
    tried = []

    def chat(model):
        tried.append(model)
        if model == "model-a":
            raise RuntimeError("404 NOT_FOUND: models/model-a is not found")
        return f"answer from {model}"
    assert medic.call_model(chat, "model-a") == "answer from model-b" and tried == ["model-a", "model-b"]

    def bad_request(model):
        raise ValueError("invalid tool schema")             # a real bug: no fallback hides it
    with pytest.raises(ValueError):
        medic.call_model(bad_request, "model-a")
