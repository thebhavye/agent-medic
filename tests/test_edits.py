import pytest

from bisect_agent.edits import EditError, apply_edits


def test_applies_all_or_nothing(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\ny = 2\n")
    with pytest.raises(EditError):
        apply_edits(tmp_path, [{"path": "a.py", "find": "x = 1", "replace": "x = 10"},
                               {"path": "a.py", "find": "missing", "replace": ""}])
    assert (tmp_path / "a.py").read_text() == "x = 1\ny = 2\n"

    apply_edits(tmp_path, [{"path": "a.py", "find": "x = 1", "replace": "x = 10"}])
    assert (tmp_path / "a.py").read_text() == "x = 10\ny = 2\n"


def test_rejects_ambiguous_and_outside_paths(tmp_path):
    (tmp_path / "a.py").write_text("a\na\n")
    with pytest.raises(EditError, match="2 times"):
        apply_edits(tmp_path, [{"path": "a.py", "find": "a", "replace": "b"}])
    with pytest.raises(EditError, match="outside"):
        apply_edits(tmp_path, [{"path": "../evil.py", "find": "a", "replace": "b"}])
