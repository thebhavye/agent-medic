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


def test_a_dispatch_entry_that_names_the_target_is_on_target(tmp_path):
    # The live bad_release run: the fix for "tool_handler search_docs" is its dispatch entry,
    # far from the def. It names the target, so it is on target; rewriting the whole table is not.
    (tmp_path / "target_agent").mkdir()
    (tmp_path / "target_agent" / "tools.py").write_text(
        "def search_docs(query):\n    return query\n" + "\n" * 20 +
        'TOOL_HANDLERS = {\n    "search_docs": lambda args: search_docs(args["query"]),\n'
        '    "read_doc": lambda args: read_doc(args["doc_id"]),\n}\n')
    target = surface("tool_handler", "search_docs", start=1, end=2)
    entry = [{"path": "target_agent/tools.py", "find": '    "search_docs": lambda args: search_docs(args["query"]),',
              "replace": '    "search_docs": lambda args: search_docs(args["q"]),'}]
    assert not any("outside the target" in p for p in tournament.review(entry, tmp_path, target))
    table = [{"path": "target_agent/tools.py",
              "find": 'TOOL_HANDLERS = {\n    "search_docs": lambda args: search_docs(args["query"]),\n'
                      '    "read_doc": lambda args: read_doc(args["doc_id"]),',
              "replace": 'TOOL_HANDLERS = {\n    "search_docs": lambda args: search_docs(args["q"]),\n'
                         '    "read_doc": lambda args: read_doc(args["doc_id"]),'}]
    assert any("outside the target" in p for p in tournament.review(table, tmp_path, target))


def test_soundness_runs_on_the_patched_file(tmp_path):
    (tmp_path / "a.py").write_text("import json\nx = json.dumps(1)\n")
    assert tournament.soundness([{"path": "a.py", "find": "", "replace": ""}], tmp_path) == []
    (tmp_path / "a.py").write_text("import json\nx = json.not_a_function(1)\n")
    assert any("json.not_a_function" in p for p in tournament.soundness([{"path": "a.py"}], tmp_path))


def test_quoting_a_whole_prompt_to_change_two_lines_is_proportionate(tmp_path):
    # The real case: the fixer quotes the 12-line prompt template and restores two of its lines.
    old = ["PROMPT = \"\"\"", "Answer using only the following context:", "", "{context}", "", "---", "",
           "If the context does not state the answer, reply: Not in the documents.",
           "Do not use outside knowledge or guess.", "", "Question: {question}", "\"\"\""]
    new = old[:7] + ["Answer the question based on the above context: {question}", "\"\"\""]
    (tmp_path / "query_data.py").write_text("\n".join(old) + "\n")
    target = surface("prompt", "PROMPT", "query_data.py", 1, 12)
    fix = [{"path": "query_data.py", "find": "\n".join(old), "replace": "\n".join(new)}]
    assert tournament.review(fix, tmp_path, target) == []


def test_changed_lines_counts_only_what_differs():
    from bisect_agent.edits import changed_lines
    assert changed_lines([{"find": "a\nb\nc", "replace": "a\nB\nc"}]) == 1
    assert changed_lines([{"find": "a", "replace": "a\nb\nc"}]) == 2
    assert changed_lines([{"find": "a\nb\nc\nd", "replace": "a"}]) == 3
