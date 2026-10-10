import pytest

from bisect_agent.search import bisect

COMMITS = [f"c{i}" for i in range(17)]


def oracle_for(first_bad: int, calls: list):
    def oracle(sha):
        calls.append(sha)
        return "bad" if COMMITS.index(sha) >= first_bad else "good"
    return oracle


@pytest.mark.parametrize("first_bad", [1, 2, 7, 15, 16])
def test_finds_first_bad_commit(first_bad):
    calls = []
    result = bisect(COMMITS, oracle_for(first_bad, calls), {})
    assert result.culprit == COMMITS[first_bad]
    assert len(calls) <= 4          # log2(15 untested commits)


def test_reuses_known_verdicts():
    known = {}
    bisect(COMMITS, oracle_for(9, []), known)
    calls = []
    result = bisect(COMMITS, oracle_for(9, calls), known)
    assert result.culprit == "c9"
    assert calls == []              # a resumed run re-tests nothing


def test_skips_untestable_commits():
    def oracle(sha):
        if sha in ("c8", "c9"):
            return "skip"
        return "bad" if COMMITS.index(sha) >= 5 else "good"
    assert bisect(COMMITS, oracle, {}).culprit == "c5"


def test_ambiguous_when_skips_hide_the_boundary():
    def oracle(sha):
        if sha == "c5":
            return "skip"
        return "bad" if COMMITS.index(sha) >= 5 else "good"
    result = bisect(COMMITS, oracle, {})
    assert result.culprit is None
    assert result.candidates == ["c5", "c6"]
