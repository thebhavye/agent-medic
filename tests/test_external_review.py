"""Review gate and failure helpers: real subprocess checks, no network, no models."""

import sys

from medic import external
from medic.external import QUOTA, TRANSIENT, Case, _public, describe_failure, fault_class, review


def spec_for(root):
    (root / "sub").mkdir(parents=True, exist_ok=True)
    return {"repo": root, "subdir": "sub", "python": sys.executable}


def case():
    return Case("test-target",
                    decision={"cause": "provider_model_retired",
                              "remedy": {"kind": "update_model", "from": "old", "to": "new"},
                              "rationale": []})


def edit(find, replace):
    return {"path": "sub/agents.py", "find": find, "replace": replace}


def test_review_flags_over_proportionate_fix(tmp_path):
    spec = spec_for(tmp_path)
    (tmp_path / "sub" / "agents.py").write_text('MODEL = "old"\n', encoding="utf-8")
    big = edit('MODEL = "old"\n', "".join(f"EXTRA_{i} = {i}\n" for i in range(10)))
    problems = review(spec, case(), [big])
    assert len(problems) == 1 and "update_model" in problems[0] and "10 lines" in problems[0]


def test_review_flags_new_broad_except(tmp_path):
    spec = spec_for(tmp_path)
    (tmp_path / "sub" / "agents.py").write_text('MODEL = "old"\n', encoding="utf-8")
    wrapped = edit('MODEL = "old"\n',
                   'try:\n    MODEL = "new"\nexcept Exception:\n    pass\n')
    problems = review(spec, case(), [wrapped])
    assert len(problems) == 1 and "broad except" in problems[0]


def test_review_flags_missing_module_attribute(tmp_path):
    spec = spec_for(tmp_path)
    (tmp_path / "sub" / "agents.py").write_text(
        'import json\nprint(json.no_such_attr_xyz)\nMODEL = "old"\n', encoding="utf-8")
    problems = review(spec, case(), [edit('MODEL = "old"\n', 'MODEL = "new"\n')])
    assert len(problems) == 1 and "json.no_such_attr_xyz" in problems[0]


def test_review_passes_one_line_model_change(tmp_path):
    spec = spec_for(tmp_path)
    (tmp_path / "sub" / "agents.py").write_text('MODEL = "old"\n', encoding="utf-8")
    assert review(spec, case(), [edit('MODEL = "old"\n', 'MODEL = "new"\n')]) == []


def test_fault_class_picks_span_type_and_status():
    assert fault_class({"type": "LLM", "message": "503 UNAVAILABLE"}) == ("LLM", "503")
    assert fault_class({"type": "TOOL", "message": "boom"}) == ("TOOL", None)


def test_describe_failure():
    run = {"exit": 1, "trace_status": "ERROR",
           "errors": [{"type": "LLM", "span": "gen", "message": "retired"}],
           "stderr_tail": "tail"}
    assert describe_failure(run) == "exit 1, trace ERROR: LLM gen: retired"
    quiet = {"exit": 1, "trace_status": "OK", "errors": [], "stderr_tail": "x" * 300}
    assert describe_failure(quiet).endswith("x" * 200)


def test_public_strips_paths_but_keeps_names():
    assert _public("C:\\Users\\dev\\code\\agents.py blew up") == "agents.py blew up"
    assert _public("/home/dev/code/agents.py blew up") == "agents.py blew up"


def test_transient_and_quota_regexes():
    assert TRANSIENT.search("503 UNAVAILABLE")
    quota_msg = "429 RESOURCE_EXHAUSTED: quota exceeded for key"
    assert TRANSIENT.search(quota_msg) and QUOTA.search(quota_msg)
    assert not TRANSIENT.search("404 NOT_FOUND") and not QUOTA.search("404 NOT_FOUND")
    assert TRANSIENT.search("operation timed out") and not QUOTA.search("operation timed out")
