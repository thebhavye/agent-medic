# agent-bisect

`git bisect` for AI agent behavior. Your agent got worse: more failures, higher cost, more
tool calls. agent-bisect finds the commit that caused it, asks Entire why that commit was
made, writes a fix that keeps the original intent, and proves the fix by re-running the
eval and reading the traces back from neatlogs.

```
detect ──► bisect ──► explain ──► fix ──► verify ──► commit to fix branch
  ▲                                          │ rejected: feed back, retry (max 3)
  └──────────── next regression ◄────────────┘
```

## How it works

1. **Detect.** Run the eval set at the known-good commit and at the current tip. Compare
   error rate, pass rate, cost per run and tool calls per run. Each metric that moved past
   its threshold is a regression, investigated one at a time.
2. **Bisect.** Binary search over the commits between good and bad. Each step checks out a
   commit in a throwaway git worktree, runs the eval as a subprocess with its own neatlogs
   session id, then reads that session's traces back through the neatlogs public API
   (`GET /api/v1/public/traces?sessionId=...`) for cost, tokens and tool calls. A commit is
   bad when its value is closer to the bad end than the good end, which keeps the search
   stable under normal LLM noise. Commits that can't be evaluated are skipped, like
   `git bisect skip`.
3. **Explain.** For the culprit, read the `Entire-Checkpoint:` trailer and run
   `entire checkpoint explain` and `entire why <file>:<line>` on the changed lines. That
   returns the prompt and session that produced the change: what it was trying to do.
4. **Fix.** Claude gets the culprit diff, the intent from Entire, the current files and the
   failing eval cases, and returns search-and-replace edits as structured output.
5. **Verify.** Apply the edits on the fix branch, re-run the eval and accept the fix only if
   the regressed metric is back near baseline *and* no other metric got worse. Otherwise
   reset, send the result back to the fixer and try again.
6. **Repeat.** Detect again on the new tip. When bisecting a later regression, earlier fixes
   are overlaid on every older commit that contains their culprit, so each regression is
   isolated even when several overlap in history.

Every step saves `.bisect/state.json`; `agent-bisect run --resume` continues an
interrupted run without re-running any evaluation. Fixes go on `agent-bisect/fix-<run>`,
never on your branch.

## Quickstart

```bash
uv venv && uv pip install -e ".[dev]"
cp .env.example .env    # ANTHROPIC_API_KEY, NEATLOGS_API_KEY, NEATLOGS_TOKEN, NEATLOGS_PROJECT_ID
python scripts/seed_history.py            # plant 3 regressions in 10 commits, prints the good sha
agent-bisect run --good <sha>
agent-bisect report
```

No keys yet? `agent-bisect simulate` runs the whole loop offline on a seeded copy of the
repo: real git, bisection, overlays and verification, with the eval and fixer simulated.
See [docs/sample-report.html](docs/sample-report.html) for its output.

Other commands: `status`, `report --html`, `watch` (compare recent production traces in neatlogs and flag
a regression), `pr` (push the fix branch and open a PR with the report), `clean`.

## Layout

| Path | What |
|---|---|
| `target_agent/` | The test subject: a research agent (Claude + 2 tools) over an offline knowledge base, instrumented with neatlogs |
| `evals/cases.jsonl` | 8 questions with expected facts |
| `bisect_agent/orchestrator.py` | The agent loop and its phases |
| `bisect_agent/search.py` | Bisection with skips and resumable verdicts |
| `bisect_agent/evaluator.py` | Runs one commit in a worktree, scores it from neatlogs traces |
| `bisect_agent/entire_ctx.py` | Pulls intent from Entire checkpoints |
| `bisect_agent/fixer.py` | Claude fix proposals as structured output |
| `scripts/seed_history.py` | Builds the demo history |
| `tests/` | Unit tests plus an end-to-end run on a real git history with the LLM faked |

## Planted regressions in the demo

| Commit | Looks like | Actually does | Caught by |
|---|---|---|---|
| "Make the agent double-check facts" | a reliability fix | ~2x tool calls and cost | cost |
| "Shorten tool parameter names to save prompt tokens" | a token optimization | schema says `q`, handler reads `query`: every search fails | errors |
| "Cap read_doc output to keep the context window small" | a token optimization | cuts every document before its key facts | quality |
