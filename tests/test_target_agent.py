"""The eval set has to be answerable at the good commit and break under the planted bugs."""

import json
from pathlib import Path

from target_agent.corpus import DOCS
from target_agent.run_eval import score
from target_agent.tools import TOOL_HANDLERS, search_docs

CASES = [json.loads(line) for line in Path("evals/cases.jsonl").read_text(encoding="utf-8").splitlines() if line]


def test_every_expected_fact_is_past_the_snippet_and_the_300_char_cap():
    for case in CASES:
        for group in case["expect"]:
            positions = [doc["body"].find(alt) for doc in DOCS.values() for alt in group if alt in doc["body"]]
            assert positions, f"{case['id']}: none of {group} is in the corpus"
            assert min(positions) > 300, f"{case['id']}: {group} appears before char 300"


def test_search_finds_a_source_for_every_case():
    for case in CASES:
        hits = {h["id"] for h in search_docs(case["question"])}
        sources = {doc_id for doc_id, doc in DOCS.items()
                   if any(alt in doc["body"] for group in case["expect"] for alt in group)}
        assert hits & sources, f"{case['id']}: search returned {hits}, sources are {sources}"


def test_score_requires_every_group():
    case = {"expect": [["Narvik"], ["412"]]}
    assert score(case, {"status": "ok", "answer": "Narvik, 412 MWh"})
    assert not score(case, {"status": "ok", "answer": "Narvik"})
    assert not score(case, {"status": "error", "answer": "Narvik, 412 MWh"})


def test_score_tolerates_unicode_dashes_spaces_and_markdown():
    case = {"expect": [["Bergen-2"], ["20 MW"]]}
    assert score(case, {"status": "ok", "answer": "**Bergen‑2** with a **20 MW** pilot"})


def test_tool_handlers():
    assert TOOL_HANDLERS["read_doc"]({"doc_id": "pricing"}).endswith("$98 per kWh in 2024.")
