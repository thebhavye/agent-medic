"""Search-and-replace edits. The fixer proposes these instead of unified diffs because
they apply reliably and fail loudly when the code doesn't match."""

from pathlib import Path


class EditError(RuntimeError):
    pass


def apply_edits(root: Path, edits: list[dict]) -> None:
    """Apply every edit or none of them. Each `find` must occur exactly once in its file."""
    root = root.resolve()
    staged: dict[Path, str] = {}
    for edit in edits:
        path = (root / edit["path"]).resolve()
        if root not in path.parents:
            raise EditError(f"{edit['path']} is outside the repository")
        if not edit["find"]:
            raise EditError(f"empty find text for {edit['path']}")
        if path not in staged:
            if not path.exists():
                raise EditError(f"{edit['path']} does not exist")
            staged[path] = path.read_text(encoding="utf-8")
        count = staged[path].count(edit["find"])
        if count != 1:
            raise EditError(f"find text occurs {count} times in {edit['path']}, expected exactly 1")
        staged[path] = staged[path].replace(edit["find"], edit["replace"])
    for path, text in staged.items():
        path.write_text(text, encoding="utf-8")
