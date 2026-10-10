import subprocess
from pathlib import Path

from medic import anatomy

ROOT = Path(__file__).resolve().parent.parent


def test_research_agent_anatomy():
    s = anatomy.scan(ROOT / "target_agent", ["agent.py", "tools.py"])
    kinds = {(x.kind, x.name) for x in s}
    assert {("prompt", "system"), ("loop_limit", "max_turns"), ("tool_schema", "search_docs"),
            ("tool_schema", "read_doc"), ("tool_handler", "search_docs"), ("client", "_get")} <= kinds
    schema = next(x for x in s if x.kind == "tool_schema" and x.name == "search_docs")
    assert schema.notes["params"] == ["query"]


def test_prompt_templates_and_model_kwargs(tmp_path):
    (tmp_path / "agents.py").write_text(
        'from neatlogs import SystemPromptTemplate\n'
        'MODEL = "gemini-2.5-flash"\n'
        '_research_sys = SystemPromptTemplate("You are a research assistant. Use the web_search tool.")\n'
        'def ideation(client):\n'
        '    return client.models.generate_content(model=MODEL, contents="x")\n')
    kinds = {(x.kind, x.name) for x in anatomy.scan(tmp_path)}
    assert ("model_config", "gemini-2.5-flash") in kinds
    assert ("prompt", "_research_sys_prompt") in kinds
    assert ("client", "ideation") in kinds


def test_resolve_maps_suspects_to_surfaces():
    s = anatomy.scan(ROOT / "target_agent", ["agent.py", "tools.py"])
    hits = anatomy.resolve(s, [{"kind": "tool_handler", "name": "read_doc", "score": 0.9},
                               {"kind": "client", "name": "read_doc", "score": 0.5}])
    assert [h["kind"] for h in hits[:2]] == ["tool_handler", "tool_handler"]
    assert any(h["kind"] == "client" and h["name"] == "_get" for h in hits)


def test_changed_surfaces_pins_a_prompt_edit(tmp_path):
    git = lambda *a: subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "agent.py").write_text('MAX_TURNS = 12\n\nSYSTEM = (\n    "You are a research assistant. Answer briefly."\n)\n')
    git("add", "-A")
    git("commit", "-qm", "v1")
    (tmp_path / "agent.py").write_text('MAX_TURNS = 12\n\nSYSTEM = (\n    "You are a research assistant. Double-check every fact."\n)\n')
    git("commit", "-qam", "double-check")
    surfaces = anatomy.scan(tmp_path)
    changed = anatomy.changed_surfaces(tmp_path, "HEAD", surfaces)
    assert [(c["kind"], c["name"]) for c in changed["surfaces"]] == [("prompt", "system")]
