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
