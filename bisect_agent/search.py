"""Binary search for the first bad commit, with skips, like `git bisect`.

The oracle is the expensive part (a full eval run), so verdicts already in `known` are
reused. That is what lets an interrupted bisection resume without re-running anything.
"""

from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class BisectResult:
    culprit: str | None             # first bad commit, or None if skips left it ambiguous
    candidates: list[str]           # the range still in doubt when ambiguous
    tested: int                     # oracle calls made in this call (not counting known verdicts)


def bisect(commits: list[str], oracle: Callable[[str], str], known: dict[str, str]) -> BisectResult:
    """commits[0] is known good and commits[-1] known bad. oracle(sha) -> good | bad | skip."""
    if len(commits) < 2:
        raise ValueError("need at least a good and a bad commit")
    lo, hi = 0, len(commits) - 1
    tested = 0
    while hi - lo > 1:
        mid = _pick(commits, lo, hi, known)
        if mid is None:
            return BisectResult(None, commits[lo + 1:hi + 1], tested)
        sha = commits[mid]
        verdict = known.get(sha)
        if verdict is None:
            verdict = oracle(sha)
            known[sha] = verdict
            tested += 1
        if verdict == "bad":
            hi = mid
        elif verdict == "good":
            lo = mid
    return BisectResult(commits[hi], [commits[hi]], tested)


def _pick(commits: list[str], lo: int, hi: int, known: dict[str, str]) -> int | None:
    """The index nearest the midpoint of (lo, hi) that is not marked skip."""
    mid = (lo + hi) // 2
    for offset in range(hi - lo):
        for idx in (mid - offset, mid + offset):
            if lo < idx < hi and known.get(commits[idx]) != "skip":
                return idx
    return None
