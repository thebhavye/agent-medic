"""Run the research agent over an eval set and write per-case results as JSON.

agent-bisect runs this inside a git worktree of the commit under test, so this file must
stay importable at every commit. Each case becomes one neatlogs trace in the given session.

    python -m target_agent.run_eval --cases evals/cases.jsonl --out results.json \
        --session bisect-abc123 --tag sha:abc123
"""

import argparse
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv

load_dotenv()

import neatlogs  # noqa: E402


def normalize(text: str) -> str:
    """Lowercase, with Unicode dashes and spaces folded to ASCII and Markdown emphasis removed.
    Models often write non-breaking hyphens ("Bergen‑2") and narrow spaces ("20 MW")."""
    text = re.sub(r"[‐-―−]", "-", text)
    text = re.sub(r"[  -​  　]", " ", text)
    return text.replace("*", "").lower()


def score(case: dict, result: dict) -> bool:
    """A case passes when the agent answered and every expected fact group matched."""
    if result["status"] != "ok":
        return False
    answer = normalize(result["answer"])
    return all(any(normalize(alt) in answer for alt in group) for group in case["expect"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--session", required=True, help="neatlogs session id for every case trace")
    parser.add_argument("--tag", action="append", default=[])
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    neatlogs.init(workflow_name="research-agent", tags=["agent-medic", *args.tag])

    # Import after init so the model client is instrumented.
    from target_agent import agent, llm

    provider = llm.get_provider()
    with open(args.cases, encoding="utf-8") as f:
        cases = [json.loads(line) for line in f if line.strip()]

    def run_case(case: dict) -> dict:
        with neatlogs.trace(f"case:{case['id']}", session_id=args.session, case_id=case["id"]):
            try:
                result = agent.run_agent(case["question"], provider)
            except Exception as exc:  # an API failure is a failed case, not a crashed eval
                result = {"status": "error", "answer": "", "error": f"{type(exc).__name__}: {exc}",
                          "turns": 0, "tool_calls": 0, "tool_errors": 0, "input_tokens": 0,
                          "output_tokens": 0, "cost_usd": 0.0, "duration_s": 0.0}
        return {"id": case["id"], "question": case["question"], "expect": case["expect"],
                "passed": score(case, result), **result}

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(run_case, cases))

    neatlogs.flush()
    neatlogs.shutdown()

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"session": args.session, "provider": llm.provider_name(), "model": agent.MODEL, "cases": results}, f, indent=2)

    passed = sum(r["passed"] for r in results)
    print(f"{passed}/{len(results)} cases passed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
