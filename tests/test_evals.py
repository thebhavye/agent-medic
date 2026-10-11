"""Known-answer cases built from what already worked: no tests to write."""

import json
from types import SimpleNamespace

from medic import evals, paths


def test_facts_are_the_checkable_parts_not_the_question():
    answer = 'The Pro plan costs $99 a month and includes 10 GB, per the "Pricing" doc. Alice works at Acme Labs.'
    assert evals.facts(answer, "How much does the Pro plan cost?") == [["99"], ["10"], ["Pricing"]]
    assert evals.facts("It is in Narvik, Norway.", "Where is Narvik?") == [["Norway"]]   # Narvik repeats the question
    assert evals.facts("It is in Narvik.", "Where is Narvik?") == []


def test_non_answers_and_fact_free_answers_make_no_case():
    assert evals.case("q?", "I could not find that in the documents.") is None
    assert evals.case("q?", "it depends on many things") is None                  # could never fail
    c = evals.case("Who does Alice meet first?", "Alice first meets the White Rabbit.")
    assert c["expect"] == [["White Rabbit"]] and c["id"] == "who-does-alice-meet-first"


def test_from_traces_keeps_healthy_production_answers(monkeypatch):
    from medic import evidence
    traces = [{"traceId": f"t{i}", "startedAt": f"2026-10-11T10:0{i}:00Z", "tags": ["env:prod"],
               "finalizationStatus": "finalized", "status": "OK"} for i in range(4)]
    traces.append({"traceId": "canary", "startedAt": "2026-10-11T10:09:00Z", "tags": ["canary"],
                   "finalizationStatus": "finalized", "status": "OK"})                # not production
    answers = {"t0": ("Where is the pilot plant?", "The pilot plant is in Narvik."),
               "t1": ("Where is the pilot plant?", "The pilot plant is in Narvik."),       # duplicate question
               "t2": ("What is the output?", "I could not find that."),                    # failed
               "t3": ("Who leads it?", "Ingrid Solberg leads the pilot.")}

    def collect(reader, trace):
        q, a = answers[trace["traceId"]]
        return SimpleNamespace(trace_id=trace["traceId"], release="r1", question=q, answer=a,
                               failed="could not" in a)
    monkeypatch.setattr(evidence, "collect", collect)
    reader = SimpleNamespace(list_traces=lambda **kw: traces)
    asked = []

    def answer_now(questions, runs):     # answered once in production: the current code is asked again
        asked.extend(questions)
        return {q: [{"status": "ok", "answer": "Ingrid Solberg runs the pilot."}] for q in questions}
    monkeypatch.setattr(evals, "_answer", answer_now)
    cases = evals.from_traces(reader)
    assert asked == ["Who leads it?"]                                   # Narvik was already seen twice
    assert [c["question"] for c in cases] == ["Who leads it?", "Where is the pilot plant?"]
    assert cases[0]["expect"] == [["Ingrid Solberg"]] and cases[1]["expect"] == [["Narvik"]]
    monkeypatch.setattr(evals, "_answer", lambda questions, runs:
                        {q: [{"status": "ok", "answer": "Lars Berg leads it."}] for q in questions})
    skipped = []
    assert [c["question"] for c in evals.from_traces(reader, log=skipped.append)] == ["Where is the pilot plant?"]
    assert "disagree" in skipped[0]


def test_write_makes_ids_unique(tmp_path):
    path = evals.write([{"id": "a", "question": "x"}, {"id": "a", "question": "y"}], tmp_path / "e.jsonl")
    assert [json.loads(line)["id"] for line in path.read_text().splitlines()] == ["a", "a-2"]


def test_from_questions_runs_the_agent_once(tmp_path, monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        out = cmd[cmd.index("--out") + 1]
        cases = [json.loads(line) for line in open(cmd[cmd.index("--cases") + 1], encoding="utf-8")]
        with open(out, "w", encoding="utf-8") as f:
            json.dump({"cases": [{**c, "status": "ok", "answer": "The Queen of Hearts rules it."} for c in cases]}, f)
    monkeypatch.setattr(evals.subprocess, "run", fake_run)
    monkeypatch.setattr(paths, "DEPLOY", tmp_path)
    cases = evals.from_questions(["Who rules Wonderland?"])
    assert cases[0]["expect"] == [["Queen of Hearts"]] and calls[0][1:3] == paths.RUNNER[:2]


def test_facts_read_model_typography():
    answer = "She plays **croquet**; either way is “mad”: the Mad Hatter or the March Hare."
    assert evals.facts(answer, "What game?") == [["croquet"], ["Mad Hatter"], ["March Hare"]]
    assert evals.facts("It says **“DRINK ME.”**", "What is on the bottle?") == [["DRINK ME"]]
    assert evals.facts("The Cheshire Cat points; the Cat grins.", "Who?") == [["Cheshire Cat"]]


def test_from_questions_keeps_only_the_facts_every_answer_agrees_on(tmp_path, monkeypatch):
    answers = {"q0-r0": "The White Rabbit is late for the Queen.", "q0-r1": "He is late for a very important date.",
               "q1-r0": "It is labelled **DRINK ME** by the Queen.", "q1-r1": "The label says DRINK ME."}

    def fake_run(cmd, **kw):
        cases = [json.loads(line) for line in open(cmd[cmd.index("--cases") + 1], encoding="utf-8")]
        with open(cmd[cmd.index("--out") + 1], "w", encoding="utf-8") as f:
            json.dump({"cases": [{**c, "status": "ok", "answer": answers[c["id"]]} for c in cases]}, f)
    monkeypatch.setattr(evals.subprocess, "run", fake_run)
    monkeypatch.setattr(paths, "DEPLOY", tmp_path)
    skipped = []
    cases = evals.from_questions(["Who is the White Rabbit late for?", "What is on the bottle?"], log=skipped.append, runs=2)
    assert [c["expect"] for c in cases] == [[["DRINK ME"]]]                # "Queen" only sometimes: dropped
    assert "disagree" in skipped[0]


def test_a_fact_ending_in_punctuation_is_kept_once():
    assert [g[0] for g in evals.facts("It asks “Who are you?” and *Who are you?*", "What?")].count("Who are you?") == 1


def test_scoring_matches_whole_facts():
    from medic.runner import score
    ok = {"status": "ok", "answer": "The Pro plan costs 199 dollars; ask Annabelle."}
    assert not score({"expect": [["99"]]}, ok) and not score({"expect": [["Ann"]]}, ok)
    assert score({"expect": [["199"], ["Annabelle"]]}, ok)
    assert score({"expect": [["Who are you?"]]}, {"status": "ok", "answer": "It asks: “Who are you?”"})


def test_an_open_question_with_several_right_answers_accepts_any():
    asks = ["The Caterpillar asks Alice, “Who are you?”",
            "It asks her “What size do you want to be?”",
            "The Caterpillar asks, “Who are you?”"]
    c = evals.case("What does the Caterpillar ask Alice?", asks[0], tuple(asks[1:]), any_of=True)
    assert c["expect"] == [["Who are you?", "What size do you want to be?"]]
    from medic.runner import score
    for a in asks:
        assert score(c, {"status": "ok", "answer": a})                      # every right answer passes
    assert not score(c, {"status": "ok", "answer": "Unable to find matching results."})
    assert evals.case("What does the Caterpillar ask Alice?", asks[0], ("I could not find that.",), any_of=True) is None
    assert evals.case("What does the Caterpillar ask Alice?", asks[0], tuple(asks[1:])) is None   # strict by default
