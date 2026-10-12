"""Search-and-replace edits. The fixer proposes these instead of unified diffs because
they apply reliably and fail loudly when the code doesn't match."""

import difflib
from pathlib import Path


class EditError(RuntimeError):
    pass


def changed_lines(edits: list[dict]) -> int:
    """How many lines the edits actually change. A `find` quotes enough to match exactly once,
    so an edit that rewrites one line of a quoted 12-line prompt changes one line, not 12."""
    total = 0
    for e in edits:
        old, new = e["find"].splitlines(), e["replace"].splitlines()
        for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=old, b=new, autojunk=False).get_opcodes():
            if op != "equal":
                total += max(i2 - i1, j2 - j1)
    return total


def apply_edits(root: Path, edits: list[dict]) -> None:
    """Apply every edit or none of them. Each `find` must occur exactly once in its file.

    Matching is done on \\n line endings, and each file is written back with the endings it
    had, so an edit never turns into a whole-file line-ending diff (Windows text mode would
    otherwise write \\r\\n everywhere).
    """
    root = root.resolve()
    staged: dict[Path, str] = {}
    endings: dict[Path, str] = {}
    for edit in edits:
        path = (root / edit["path"]).resolve()
        if root not in path.parents:
            raise EditError(f"{edit['path']} is outside the repository")
        if not edit["find"]:
            raise EditError(f"empty find text for {edit['path']}")
        if path not in staged:
            if not path.exists():
                raise EditError(f"{edit['path']} does not exist")
            raw = path.read_bytes().decode("utf-8")
            endings[path] = "\r\n" if "\r\n" in raw else "\n"
            staged[path] = raw.replace("\r\n", "\n")
        find = edit["find"].replace("\r\n", "\n")
        count = staged[path].count(find)
        if count != 1:
            raise EditError(f"find text occurs {count} times in {edit['path']}, expected exactly 1")
        staged[path] = staged[path].replace(find, edit["replace"].replace("\r\n", "\n"))
    for path, text in staged.items():
        path.write_bytes(text.replace("\n", endings[path]).encode("utf-8"))
