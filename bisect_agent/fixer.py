"""Ask the model for a minimal fix to the culprit commit, as search-and-replace edits."""

import json
from pathlib import Path

import neatlogs

from bisect_agent import gitops
from bisect_agent.metrics import SIGNATURES
from target_agent import llm

MAX_DIFF_CHARS = 60_000
MAX_FAILING_CASES = 4

SYSTEM = """You fix behavior regressions in AI agents.

You get: a regression measured by an eval (which metric got worse and by how much), the
commit that bisection proved introduced it, the record of why that commit was made (often
the agent session that wrote it, from Entire), the current version of the files it touched,
and examples of failing eval cases.

Write the smallest change to the current code that removes the regression. Keep the goal of
the original commit where you can: if it was a deliberate optimization or refactor, repair
how it was done rather than reverting it outright, unless the goal itself causes the
regression. Do not change the eval, the test cases or unrelated code.

Return edits as exact search-and-replace pairs. Each `find` must be copied verbatim from the
current file and must occur exactly once in it; include enough surrounding lines to make it
unique."""

FIX_SCHEMA = {
    "type": "object",
    "properties": {
        "diagnosis": {"type": "string", "description": "How the culprit change causes the regression."},
        "original_intent": {"type": "string", "description": "What the culprit commit was trying to achieve."},
        "how_intent_is_kept": {"type": "string", "description": "How the fix keeps that goal, or why it can't."},
        "commit_title": {"type": "string", "description": "Short imperative commit title for the fix."},
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "find": {"type": "string"},
                    "replace": {"type": "string"},
                },
                "required": ["path", "find", "replace"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["diagnosis", "original_intent", "how_intent_is_kept", "commit_title", "edits"],
    "additionalProperties": False,
}


class FixerError(RuntimeError):
    pass


def build_prompt(*, regression: str, signature: str, culprit: str, repo: Path, tip_root: Path,
                 intent_text: str, failing_cases: list[dict], previous_attempts: list[dict]) -> str:
    diff = gitops.show(culprit, repo)
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + f"\n[diff cut at {MAX_DIFF_CHARS} characters of {len(diff)}]"

    files = []
    for path in gitops.changed_files(culprit, repo):
        current = tip_root / path
        if current.exists():
            files.append(f'<file path="{path}">\n{current.read_text(encoding="utf-8")}\n</file>')
        else:
            files.append(f'<file path="{path}" deleted_at_tip="true"/>')

    cases = []
    for c in failing_cases[:MAX_FAILING_CASES]:
        cases.append(json.dumps({k: c.get(k) for k in
                                 ("id", "question", "expect", "status", "answer", "error",
                                  "tool_calls", "tool_errors", "turns", "cost_usd")}, indent=1))

    sections = [
        f"<regression signature=\"{signature}\" metric=\"{SIGNATURES[signature].metric}\">\n{regression}\n</regression>",
        f"<culprit_commit>\n{diff}\n</culprit_commit>",
        f"<why_it_was_made>\n{intent_text}\n</why_it_was_made>",
        "<current_files>\n" + "\n".join(files) + "\n</current_files>",
        "<eval_cases_at_culprit>\n" + "\n".join(cases) + "\n</eval_cases_at_culprit>",
    ]
    for i, attempt in enumerate(previous_attempts, 1):
        sections.append(f"<previous_attempt n=\"{i}\">\nedits: {json.dumps(attempt.get('edits'))}\n"
                        f"result: {attempt.get('feedback')}\n</previous_attempt>")
    if previous_attempts:
        sections.append("Your previous attempts did not pass verification. Try a different fix.")
    return "\n\n".join(sections)


@neatlogs.span(kind="AGENT", name="fixer")
def propose_fix(prompt: str) -> dict:
    model = llm.model_for("fixer")
    try:
        fix, input_tokens, output_tokens = llm.get_provider("fixer").complete_json(model, SYSTEM, prompt, FIX_SCHEMA)
    except (ValueError, KeyError) as exc:
        raise FixerError(f"no usable fix proposal: {exc}") from exc
    if not isinstance(fix.get("edits"), list):
        raise FixerError("fix proposal has no edits list")
    fix["usage"] = {"model": model, "input_tokens": input_tokens, "output_tokens": output_tokens}
    return fix
