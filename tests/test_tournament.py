from types import SimpleNamespace

from medic import tournament


def surface(kind, name, file="target_agent/tools.py", start=10, end=12, snippet="x = 1"):
    return {"kind": kind, "name": name, "file": file, "start": start, "end": end, "symbol": name,
            "snippet": snippet}


def test_targets_put_agreement_first_and_dedupe(monkeypatch):
    inc = SimpleNamespace(
        decision={"remedy": "code_fix"},
        findings={"regression": {"changed_surfaces": [surface("prompt", "system", "target_agent/agent.py", 16, 21),
                                                      surface("tool_schema", "search_docs")]},
                  "localize": {"surfaces": [surface("tool_schema", "search_docs"),
                                            surface("tool_handler", "search_docs", start=43, end=44)]}})
    picked = tournament.targets(inc)
    assert [(t["kind"], t["name"]) for t in picked] == [("tool_schema", "search_docs"), ("prompt", "system"),
                                                        ("tool_handler", "search_docs")]


def test_review_keeps_candidates_on_target(tmp_path):
    (tmp_path / "target_agent").mkdir()
    (tmp_path / "target_agent" / "tools.py").write_text("def read_doc(d):\n    return d\n")
    target = surface("tool_handler", "read_doc", start=1, end=2)
    ok = [{"path": "target_agent/tools.py", "find": "    return d", "replace": "    return d.strip()"}]
    assert tournament.review(ok, tmp_path, target) == []
    off = [{"path": "target_agent/agent.py", "find": "a", "replace": "b"}]
    assert "outside the target file" in tournament.review(off, tmp_path, target)[0]
    hiding = [{"path": "target_agent/tools.py", "find": "    return d",
               "replace": "    try:\n        return d\n    except Exception:\n        return ''"}]
    assert any("broad except" in p for p in tournament.review(hiding, tmp_path, target))
    huge = [{"path": "target_agent/tools.py", "find": "    return d", "replace": "\n".join(["    pass"] * 30)}]
    assert any("should need about" in p for p in tournament.review(huge, tmp_path, target))


def test_target_section_names_the_part_and_operator():
    text = tournament.target_section(surface("prompt", "system", "target_agent/agent.py", 16, 21, "SYSTEM = (...)"))
    assert 'kind="prompt"' in text and "Edit the instruction text" in text and "SYSTEM = (...)" in text


def test_review_rejects_routing_around_a_suspected_function(tmp_path):
    (tmp_path / "target_agent").mkdir()
    (tmp_path / "target_agent" / "tools.py").write_text(
        'def read_doc(doc_id):\n    return _get(doc_id)["body"][:300]\n\n'
        'TOOL_HANDLERS = {"read_doc": lambda args: read_doc(args["doc_id"])}\n')
    target = surface("tool_handler", "read_doc", start=4, end=4)
    bypass = [{"path": "target_agent/tools.py", "find": 'lambda args: read_doc(args["doc_id"])',
               "replace": 'lambda args: _get(args["doc_id"])["body"]'}]
    suspects = [surface("tool_handler", "read_doc", start=1, end=2)]
    problems = tournament.review(bypass, tmp_path, target, suspects)
    assert any("bypasses read_doc()" in p for p in problems)
    real_fix = [{"path": "target_agent/tools.py", "find": '["body"][:300]', "replace": '["body"]'}]
    assert tournament.review(real_fix, tmp_path, surface("tool_handler", "read_doc", start=1, end=2), suspects) == []


def test_review_checks_the_target_lines_not_just_the_file(tmp_path):
    (tmp_path / "target_agent").mkdir()
    (tmp_path / "target_agent" / "tools.py").write_text(
        "SCHEMA = {'name': 'x'}\n" + "\n" * 20 + "def handler(a):\n    return a\n")
    schema = surface("tool_schema", "x", start=1, end=1)
    handler_edit = [{"path": "target_agent/tools.py", "find": "    return a", "replace": "    return a.strip()"}]
    assert any("outside the target" in p for p in tournament.review(handler_edit, tmp_path, schema))


def test_soundness_runs_on_the_patched_file(tmp_path):
    (tmp_path / "a.py").write_text("import json\nx = json.dumps(1)\n")
    assert tournament.soundness([{"path": "a.py", "find": "", "replace": ""}], tmp_path) == []
    (tmp_path / "a.py").write_text("import json\nx = json.not_a_function(1)\n")
    assert any("json.not_a_function" in p for p in tournament.soundness([{"path": "a.py"}], tmp_path))
