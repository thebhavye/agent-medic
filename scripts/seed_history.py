"""Build a demo commit history with three planted regressions among harmless commits.

Run from a clean checkout of this repo. Each step becomes one commit; the commit you start
on is the known-good baseline.

    python scripts/seed_history.py
    agent-bisect run --good <printed sha>

The regressions are the kind an AI coding agent makes while "optimizing":
  1. cost:    a prompt change that makes the agent re-verify every fact (tool calls ~2x)
  2. errors:  a tool parameter renamed in the schema but not in the handler
  3. quality: read_doc output capped at 300 characters, cutting off the facts

For the full demo, make the three regression commits with Claude Code and Entire enabled
instead (see docs/DEMO.md), so `entire checkpoint explain` has a real session to return.
"""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent  # overridden by --repo

STEPS = [
    {
        "message": "docs: describe the target research agent",
        "write": {"docs/target-agent.md": "# Target agent\n\nA research agent over the Halcyon "
                  "knowledge base, used as agent-bisect's test subject.\n"},
    },
    {
        "message": "Give the knowledge base API more time to respond",
        "edits": [("target_agent/tools.py", "TIMEOUT_S = 10", "TIMEOUT_S = 15")],
    },
    {
        "message": "Make the research agent double-check facts before answering\n\n"
                   "Answers occasionally cited the wrong figure. Have the agent confirm each fact "
                   "against its source before it answers.",
        "edits": [("target_agent/agent.py",
                   '    "Finish with a concise answer that states the specific names and figures asked for."\n',
                   '    "Before answering, double-check every fact: search for it again with different "\n'
                   '    "keywords and re-read each source document to confirm it. Finish with a concise "\n'
                   '    "answer that states the specific names and figures asked for."\n')],
    },
    {
        "message": "Add careers page to the knowledge base",
        "edits": [("target_agent/corpus.py",
                   '            "compounds from returned cells."\n        ),\n    },\n}',
                   '            "compounds from returned cells."\n        ),\n    },\n'
                   '    "careers": {\n        "title": "Careers",\n        "body": (\n'
                   '            "Halcyon Grid hires engineers, technicians and operators in Norway and "\n'
                   '            "the Czech Republic. Most roles are on site at the pilot plant."\n'
                   '        ),\n    },\n}')],
    },
    {
        "message": "Document read_doc",
        "edits": [("target_agent/tools.py", "def read_doc(doc_id: str) -> str:\n",
                   'def read_doc(doc_id: str) -> str:\n    """Full text of one document."""\n')],
    },
    {
        "message": "Shorten tool parameter names to save prompt tokens\n\n"
                   "Tool schemas are sent on every turn; 'q' is shorter than 'query'.",
        "edits": [("target_agent/tools.py",
                   '                "query": {"type": "string", "description": "Keywords to search for."},\n'
                   '            },\n            "required": ["query"],',
                   '                "q": {"type": "string", "description": "Keywords to search for."},\n'
                   '            },\n            "required": ["q"],')],
    },
    {
        "message": "Rename leadership doc title",
        "edits": [("target_agent/corpus.py", '"title": "Leadership team",', '"title": "Leadership and management team",')],
    },
    {
        "message": "Docstring for run_agent",
        "edits": [("target_agent/agent.py", "def run_agent(question: str, provider) -> dict:\n",
                   "def run_agent(question: str, provider) -> dict:\n"
                   '    """Answer one question; returns the answer plus usage stats."""\n')],
    },
    {
        "message": "Cap read_doc output to keep the context window small\n\n"
                   "Long documents were inflating input tokens on every later turn.",
        "edits": [("target_agent/tools.py", '    return _get(f"/docs/{doc_id}")["body"]\n',
                   "    # Keep tool output small so long documents don't bloat the context window.\n"
                   '    return _get(f"/docs/{doc_id}")["body"][:300]\n')],
    },
    {
        "message": "docs: note the knowledge base size",
        "write": {"docs/target-agent.md": "# Target agent\n\nA research agent over the Halcyon "
                  "knowledge base (11 documents), used as agent-bisect's test subject.\n"},
    },
]


# Named releases for the live demo (agent-medic world release <name>).
SCENARIOS = {
    "double-check": 2,      # cost / tool-call regression
    "short-params": 5,      # tool schema renamed, handler not: every search fails
    "cap-docs": 8,          # read_doc cut at 300 chars: answers lose their facts
}


def git(*args: str, repo: Path | None = None) -> str:
    return subprocess.run(["git", *args], cwd=repo or REPO, check=True, capture_output=True,
                          text=True).stdout.strip()


def apply_step(repo: Path, step: dict) -> str:
    """Apply one step's file changes and commit them. Returns the new commit sha."""
    for path, content in step.get("write", {}).items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    for path, find, replace in step.get("edits", []):
        target = repo / path
        text = target.read_text(encoding="utf-8")
        if text.count(find) != 1:
            raise ValueError(f"step {step['message']!r}: expected text not found once in {path}")
        target.write_text(text.replace(find, replace), encoding="utf-8")
    git("add", "-A", repo=repo)
    git("commit", "-q", "-m", step["message"], repo=repo)
    return git("rev-parse", "HEAD", repo=repo)


def main() -> int:
    global REPO
    if "--repo" in sys.argv:
        REPO = Path(sys.argv[sys.argv.index("--repo") + 1]).resolve()
    if git("status", "--porcelain"):
        print("working tree is not clean; commit or stash first", file=sys.stderr)
        return 1
    good = git("rev-parse", "HEAD")
    for step in STEPS:
        try:
            apply_step(REPO, step)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 1
        print(f"{git('rev-parse', '--short', 'HEAD')} {step['message'].splitlines()[0]}")
    print(f"\nknown good: {good}\nrun: agent-medic bisect run --good {good[:10]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
