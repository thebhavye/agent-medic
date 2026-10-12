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

# A number with its unit when it has one: "5 business days" is a fact where a bare "5" is too
# common to check.
NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?%?"
                    r"(?: (?:business |working |calendar )?(?:days?|weeks?|months?|years?|hours?|minutes?)\b)?")
NAME = re.compile(r"\b[A-Z][a-z]+(?:(?: (?:of|the|de|von|van) |[ -])[A-Z][a-z]+)+\b|\b[A-Z]{2,}\b"
                  r"|(?<=[a-z,;:] )[A-Z][a-z]{2,}\b")       # a single name mid-sentence ("in Narvik")
QUOTED = re.compile(r"[\"“']([^\"”']{3,40})[\"”']")
EMPHASIS = re.compile(r"\*\*([^*\n]{2,40})\*\*|(?<![*\w])\*([^*\n]{2,40})\*(?![*\w])")   # the answer's key term
NON_ANSWER = re.compile(r"could not|couldn't|unable to|no information|not available|cannot find|can't find|"
                        r"don't know|do not know", re.I)
STOP = {"The", "This", "That", "These", "It", "In", "On", "As", "A", "An", "Yes", "No", "Response", "Sources"}
MAX_FACTS = 3


def facts(answer: str, question: str = "") -> list[list[str]]:
    """The answer's checkable facts, as expect groups. Facts already in the question don't count:
    an answer can repeat the question and still be wrong."""
    q = question.lower()
    answer = _plain(answer)                          # models write no-break spaces and hyphens
    out = []
    for kind in (NUMBER, QUOTED, EMPHASIS, NAME):
        for m in kind.finditer(answer):
            text = (next(g for g in m.groups() if g) if m.groups() else m.group(0)).strip(" \"“”'.,;:!*")
            if kind is NAME and " " in text and text.split(" ", 1)[0] in STOP:
                text = text.split(" ", 1)[1]          # "The Queen of Hearts" starts a sentence
            key = text.lower()
            if not text or text in STOP or key in q or len(text) < 2:
                continue
            if any(_within(key, kept[0].lower()) for kept in out):
                continue                              # "Cat" says nothing "Cheshire Cat" didn't
            out = [kept for kept in out if not _within(kept[0].lower(), key)]
            out.append([text])
    return out[:MAX_FACTS]


def _plain(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\u2011", "-"))


def _within(fact: str, text: str) -> bool:
    """`fact` appears in `text` as whole words (a fact may end in punctuation: "Who are you?")."""
    return re.search(rf"(?<!\w){re.escape(fact)}(?!\w)", text) is not None


def slug(question: str) -> str:
    words = re.findall(r"[a-z0-9]+", question.lower())[:6]
    return "-".join(words) or "q"


def case(question: str, answer: str, others: tuple = (), any_of: bool = False) -> dict | None:
    """A known-answer case, or None. With other answers to the same question, only the facts every
    answer agrees on are required: a fact the agent states only sometimes would make a flaky eval.

    An open question can have several right answers (the Caterpillar asks "Who are you?" and also
    "What size do you want to be?"). When the answers share no fact but each states one, the case
    accepts any of them (`any_of`, for answers from the same code: a production answer and today's
    disagreeing means one is wrong). A release that stops answering still fails it."""
    if not answer or NON_ANSWER.search(answer):
        return None
    expect = [g for g in facts(answer, question)
              if all(_within(g[0].lower(), _plain(o).lower()) for o in others)]
    if not expect and others and any_of:
        firsts = []
        for text in (answer, *others):
            found = facts(text, question) if text and not NON_ANSWER.search(text) else []
            if not found:
                return None            # an answer with nothing checkable: no reliable case
            if not any(_within(f.lower(), _plain(text).lower()) for f in firsts):
                firsts.append(found[0][0])
        expect = [firsts]              # one group: any of these facts (every sampled answer kept)
    if not expect:
        return None                    # nothing checkable: such a case could never fail
    return {"id": slug(question), "question": question, "expect": expect, "source_answer": answer[:400]}


def from_traces(reader, days: int = 7, limit: int = 30, log=print, confirm: bool = True) -> list[dict]:
    """Healthy production answers: finalized env:prod traces of this workflow that didn't fail.

    Every healthy answer to a question counts. A question production answered only once is asked
    of the current code once more (`confirm`), so a fact production stated only sometimes doesn't
    become a required one."""
    from medic import evidence
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    traces = [t for t in reader.list_traces(workflowName=paths.WORKFLOW, **{"from": since})
              if "env:prod" in t.get("tags", []) and t.get("finalizationStatus") == "finalized"
              and str(t.get("status", "")).upper() != "ERROR"]
    seen: dict[str, list] = {}          # question -> healthy production evidence, newest first
    for trace in sorted(traces, key=lambda t: t["startedAt"], reverse=True):
        try:
            ev = evidence.collect(reader, trace)
        except Exception:  # noqa: BLE001 - an unreadable trace is skipped
            continue
        if ev.failed or not ev.question or (ev.question not in seen and len(seen) >= limit):
            continue
        seen.setdefault(ev.question, []).append(ev)
    once = [q for q, evs in seen.items() if len(evs) < 2 and case(q, evs[0].answer)]
    fresh = _answer(once, runs=1) if confirm and once else {}
    cases = []
    for question, evs in seen.items():
        answers = [e.answer for e in evs] + [r.get("answer", "") for r in fresh.get(question, [])
                                             if r.get("status") == "ok"]
        c = case(question, answers[0], tuple(answers[1:]))
        if c:
            cases.append({**c, "trace_id": evs[0].trace_id, "release": evs[0].release})
        elif len(answers) > 1 and case(question, answers[0]):
            log(f"  skipped {question[:60]!r}: the answers disagree: "
                + " / ".join(repr(a[:60]) for a in answers[:3]))
    return cases


def _answer(questions: list[str], runs: int) -> dict[str, list[dict]]:
    """The current code's answers: question -> `runs` runner results."""
    with tempfile.TemporaryDirectory() as tmp:
        cases_in = Path(tmp) / "questions.jsonl"
        out = Path(tmp) / "answers.json"
        cases_in.write_text("".join(json.dumps({"id": f"q{i}-r{k}", "question": q, "expect": []}) + "\n"
                                    for i, q in enumerate(questions) for k in range(runs)), encoding="utf-8")
        subprocess.run([paths.PYTHON, *paths.RUNNER, "--cases", str(cases_in), "--out", str(out), "--session",
                        f"evals-bootstrap-{datetime.now():%Y%m%d-%H%M%S}", "--tag", "evals-bootstrap"],
                       cwd=paths.DEPLOY, capture_output=True, timeout=3600)
        if not out.exists():
            raise RuntimeError("the agent produced no answers; check that it runs: "
                               f"{paths.PYTHON} {' '.join(paths.RUNNER)} --cases ... --out ...")
        results = json.loads(out.read_text(encoding="utf-8"))["cases"]
    by_index: dict[str, list] = {}
    for r in results:
        by_index.setdefault(r["id"].rsplit("-r", 1)[0], []).append(r)
    return {q: by_index.get(f"q{i}", []) for i, q in enumerate(questions)}


AUTH_ERROR = re.compile(r"\b401\b|AuthenticationError|Unauthorized|invalid api key|incorrect api key", re.I)


def from_questions(questions: list[str], log=print, runs: int = 5) -> list[dict]:
    """A new app: the current code answers each question `runs` times; the facts its answers agree on
    become the expected facts."""
    by_question = _answer(questions, runs)
    cases, errors = [], []
    for question in questions:
        answers = by_question.get(question, [])
        failed = next((r for r in answers if r.get("status") != "ok"), None)
        texts = [r.get("answer", "") for r in answers]
        c = case(question, texts[0], tuple(texts[1:]), any_of=True) if answers and not failed else None
        if c:
            cases.append(c)
            continue
        if failed:                     # say why, so the user can fix the app or the question
            why = (failed.get("error") or "")[:160]
            errors.append(why)
        elif texts and case(question, texts[0]):
            why = f"the answers disagree: {' / '.join(repr(t[:60]) for t in texts)}"
        else:
            why = f"no checkable facts in the answer: {(texts[0] if texts else '(none)')[:100]!r}"
        log(f"  skipped {question[:60]!r}: {why}")
    if errors and len(errors) == len(questions) and all(AUTH_ERROR.search(e) for e in errors):
        log("your app's model provider rejected its API key on every question (401). Medic runs your app "
            "with its own .env: check the key the app reads there (for an OpenAI-compatible client, "
            "OPENAI_API_KEY), and that it isn't still a placeholder.")
    return cases


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
