"""`agent-medic evals bootstrap`: known-answer questions without writing tests.

Medic's quality checks (the canary, first-aid trials, fix verification, bisection) need
questions with known answers. Nobody writes those. So Medic takes them from what already
worked: questions the healthy agent answered, with the facts its answers contained.

    agent-medic evals bootstrap                      # from healthy production traces in neatlogs
    agent-medic evals bootstrap --questions qs.txt   # a new app: answer these once, keep the facts

The facts kept from an answer are the ones a regression loses first: numbers, names and
quoted terms. A later release that drops them fails the eval.
"""

import json
import re
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from medic import paths

NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?%?")
NAME = re.compile(r"\b[A-Z][a-z]+(?:(?: (?:of|the|de|von|van) |[ -])[A-Z][a-z]+)+\b|\b[A-Z]{2,}\b"
                  r"|(?<=[a-z,;:] )[A-Z][a-z]{2,}\b")       # a single name mid-sentence ("in Narvik")
QUOTED = re.compile(r"[\"“']([^\"”']{3,40})[\"”']")
NON_ANSWER = re.compile(r"could not|couldn't|unable to|no information|not available|cannot find|can't find|"
                        r"don't know|do not know", re.I)
STOP = {"The", "This", "That", "These", "It", "In", "On", "As", "A", "An", "Yes", "No", "Response", "Sources"}
MAX_FACTS = 3


def facts(answer: str, question: str = "") -> list[list[str]]:
    """The answer's checkable facts, as expect groups. Facts already in the question don't count:
    an answer can repeat the question and still be wrong."""
    q = question.lower()
    seen, out = set(), []
    for kind in (NUMBER, QUOTED, NAME):
        for m in kind.finditer(answer):
            text = (m.group(1) if kind is QUOTED else m.group(0)).strip()
            if kind is NAME and " " in text and text.split(" ", 1)[0] in STOP:
                text = text.split(" ", 1)[1]          # "The Queen of Hearts" starts a sentence
            key = text.lower()
            if not text or text in STOP or key in seen or key in q or len(text) < 2:
                continue
            seen.add(key)
            out.append([text])
            if len(out) >= MAX_FACTS:
                return out
    return out


def slug(question: str) -> str:
    words = re.findall(r"[a-z0-9]+", question.lower())[:6]
    return "-".join(words) or "q"


def case(question: str, answer: str) -> dict | None:
    if not answer or NON_ANSWER.search(answer):
        return None
    expect = facts(answer, question)
    if not expect:
        return None                    # nothing checkable: such a case could never fail
    return {"id": slug(question), "question": question, "expect": expect, "source_answer": answer[:400]}


def from_traces(reader, days: int = 7, limit: int = 30) -> list[dict]:
    """Healthy production answers: finalized env:prod traces of this workflow that didn't fail."""
    from medic import evidence
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    traces = [t for t in reader.list_traces(workflowName=paths.WORKFLOW, **{"from": since})
              if "env:prod" in t.get("tags", []) and t.get("finalizationStatus") == "finalized"
              and str(t.get("status", "")).upper() != "ERROR"]
    cases, questions = [], set()
    for trace in sorted(traces, key=lambda t: t["startedAt"], reverse=True):
        if len(cases) >= limit:
            break
        try:
            ev = evidence.collect(reader, trace)
        except Exception:  # noqa: BLE001 - an unreadable trace is skipped
            continue
        if ev.failed or not ev.question or ev.question in questions:
            continue
        c = case(ev.question, ev.answer)
        if c:
            questions.add(ev.question)
            cases.append({**c, "trace_id": ev.trace_id, "release": ev.release})
    return cases


def from_questions(questions: list[str]) -> list[dict]:
    """A new app: run the current code once on these questions and keep its answers' facts."""
    with tempfile.TemporaryDirectory() as tmp:
        cases_in = Path(tmp) / "questions.jsonl"
        out = Path(tmp) / "answers.json"
        cases_in.write_text("".join(json.dumps({"id": f"q{i}", "question": q, "expect": []}) + "\n"
                                    for i, q in enumerate(questions)), encoding="utf-8")
        subprocess.run([paths.PYTHON, *paths.RUNNER, "--cases", str(cases_in), "--out", str(out), "--session",
                        f"evals-bootstrap-{datetime.now():%Y%m%d-%H%M%S}", "--tag", "evals-bootstrap"],
                       cwd=paths.DEPLOY, capture_output=True, timeout=3600)
        if not out.exists():
            raise RuntimeError("the agent produced no answers; check that it runs: "
                               f"{paths.PYTHON} {' '.join(paths.RUNNER)} --cases ... --out ...")
        results = json.loads(out.read_text(encoding="utf-8"))["cases"]
    return [c for r in results if r.get("status") == "ok" and (c := case(r["question"], r.get("answer", "")))]


def write(cases: list[dict], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    ids = {}
    for c in cases:                    # unique ids
        n = ids.get(c["id"], 0)
        ids[c["id"]] = n + 1
        if n:
            c["id"] = f"{c['id']}-{n + 1}"
    path.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in cases), encoding="utf-8")
    return path
