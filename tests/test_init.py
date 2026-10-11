"""`agent-medic init` on a RAG app shaped the way people usually write one."""

import ast
import importlib
import sys
import tomllib

import pytest

from medic import init as init_mod

CHAT = '''"""A docs chatbot."""
import os

from openai import OpenAI

from app.retrieve import search

client = OpenAI(base_url=os.environ.get("BASE_URL"))
MODEL = "gpt-4o-mini"
TEMPERATURE = 0.2
SYSTEM_PROMPT = "You answer questions about the product docs. Use only the context. Cite the doc title."


def build_messages(question, chunks):
    context = "\\n\\n".join(c["text"] for c in chunks)
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Context:\\n{context}\\n\\nQuestion: {question}"}]


def answer(question: str) -> str:
    chunks = search(question)
    resp = client.chat.completions.create(model=MODEL, temperature=TEMPERATURE,
                                          messages=build_messages(question, chunks))
    return resp.choices[0].message.content


def helper_not_on_the_path():
    return 1
'''

RETRIEVE = '''TOP_K = 4
DOCS = [{"title": "Pricing", "text": "The Pro plan costs 99 dollars a month."},
        {"title": "Limits", "text": "Each project can store 10 GB."}]


def score(query, doc):
    return sum(w in doc["text"].lower() for w in query.lower().split())


def search(query, k=None):
    ranked = sorted(DOCS, key=lambda d: -score(query, d))
    return ranked[: k or TOP_K]
'''


@pytest.fixture
def app(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("")
    (tmp_path / "app" / "chat.py").write_text(CHAT)
    (tmp_path / "app" / "retrieve.py").write_text(RETRIEVE)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text("def test_x():\n    assert True\n")
    (tmp_path / "requirements.txt").write_text("openai>=1.40\n")
    return tmp_path


def test_scan_finds_the_answer_path(app):
    plan = init_mod.scan(app)
    assert (plan.entrypoint.file, plan.entrypoint.name) == ("app/chat.py", "answer")
    assert {f.name for f in plan.surfaces} == {"search", "build_messages", "score"}     # not the unused helper
    values = {(rel, name) for rel, name, _ in plan.values}
    assert {("app/chat.py", "MODEL"), ("app/chat.py", "TEMPERATURE"), ("app/chat.py", "SYSTEM_PROMPT"),
            ("app/retrieve.py", "TOP_K")} <= values
    assert not plan.has_neatlogs_init


def test_dry_run_writes_nothing_and_the_diff_is_minimal(app):
    before = (app / "app" / "chat.py").read_text()
    plan, edits, proj = init_mod.run(app, write=False)
    assert (app / "app" / "chat.py").read_text() == before and not (app / "medic.toml").exists()
    text = edits["app/chat.py"]
    assert "@medic.entrypoint\ndef answer(" in text and '@medic.surface("build_messages")' in text
    assert 'model=medic.value("model", MODEL)' in text and 'medic.value("system_prompt", SYSTEM_PROMPT)' in text
    assert 'neatlogs.init(workflow_name=' in text and '"release:" + os.environ.get("GIT_SHA"' in text
    assert "helper_not_on_the_path" in text and '@medic.surface("helper_not_on_the_path")' not in text
    assert 'MODEL = "gpt-4o-mini"' in text                                       # definitions stay as written
    for new in edits.values():
        ast.parse(new)
    assert proj.entrypoint == "app.chat:answer"


def test_write_applies_it_and_the_patched_app_still_answers(app, monkeypatch):
    plan, edits, proj = init_mod.run(app, workflow="docs-bot", write=True)
    conf = tomllib.loads((app / "medic.toml").read_text())
    assert conf["agent"]["entrypoint"] == "app.chat:answer" and conf["neatlogs"]["workflow"] == "docs-bot"
    assert set(conf["agent"]["files"]) == {"app/chat.py", "app/retrieve.py"}
    assert "agent-medic" in (app / "requirements.txt").read_text()
    # The patched retriever runs, through the runtime, exactly as before.
    monkeypatch.syspath_prepend(str(app))
    monkeypatch.setenv("MEDIC_RUNNER", "1")
    for name in [m for m in sys.modules if m == "app" or m.startswith("app.")]:
        monkeypatch.delitem(sys.modules, name)
    retrieve = importlib.import_module("app.retrieve")
    assert [d["title"] for d in retrieve.search("pro plan costs")][:1] == ["Pricing"]
    assert getattr(retrieve.search, "__wrapped__", None) is not None             # registered with the runtime


def test_running_init_twice_changes_nothing_more(app):
    init_mod.run(app, write=True)
    snapshot = {p: p.read_text() for p in app.rglob("*.py")}
    plan, edits, _ = init_mod.run(app, write=False)
    assert all(edits[rel] == (app / rel).read_text() for rel in edits)
    assert snapshot == {p: p.read_text() for p in app.rglob("*.py")}


def test_no_entrypoint_is_said_plainly(tmp_path):
    (tmp_path / "util.py").write_text("def add(a, b):\n    return a + b\n")
    plan, edits, proj = init_mod.run(tmp_path)
    assert plan.entrypoint is None and proj is None and edits == {}
    assert "no entrypoint found" in init_mod.report(plan)


def test_write_finds_the_apps_venv_and_keeps_medic_state_out_of_git(app):
    (app / ".venv").mkdir()
    (app / ".venv" / "pyvenv.cfg").write_text("home = x\n")
    (app / ".venv" / "bin").mkdir()
    (app / ".venv" / "bin" / "python").write_text("")
    (app / ".gitignore").write_text("__pycache__/")              # no trailing newline
    (app / "requirements.txt").write_text("openai\n")
    init_mod.run(app, write=True)
    assert tomllib.loads((app / "medic.toml").read_text())["agent"]["python"] == ".venv/bin/python"
    assert (app / ".gitignore").read_text().splitlines() == ["__pycache__/", ".medic/"]
    assert "agent-medic @ git+https://" in (app / "requirements.txt").read_text()
    init_mod.run(app, write=True)
    assert (app / ".gitignore").read_text().count(".medic/") == 1
