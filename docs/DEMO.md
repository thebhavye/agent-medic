# Demo runbook

## Setup (Oct 10, at the start of the build window)

```bash
git init && git add -A && git commit -m "agent-bisect: initial import"
entire enable --agent claude-code
python -m neatlogs doctor --probe --json      # proves ingest + readback with your key
```

Check that `entire why` and `entire blame` run in your build (`entire labs` lists them).

## Make the regressions with a real agent session

`scripts/seed_history.py` plants the regressions directly, which is fine for testing, but
then the culprit commits have no Entire session behind them and the explain step falls back
to the commit message. For the demo, make the three regression commits with Claude Code while
Entire is enabled, so `entire checkpoint explain` returns a real session. Ask for each change
in its own session and commit, mixed in with some harmless commits:

1. "Answers sometimes cite the wrong figure. Make the research agent double-check every fact
   by searching again and re-reading the source before it answers."
2. "Tool schemas are sent on every turn. Shorten the search_docs parameter name to save
   tokens." (Ask it to only touch the schema, which is how the bug slips in.)
3. "Long documents inflate input tokens on every turn. Cap read_doc output at 300 characters."

## The 3-minute story

1. The agent works: run the eval at the good commit and show the neatlogs traces.
2. Ten commits later it's worse: `agent-bisect watch` or the first detect step flags error
   rate, pass rate and cost.
3. `agent-bisect run --good <sha>`: show bisection narrowing the range, each step's traces
   in neatlogs under its own session.
4. Culprit found: show the `entire checkpoint explain` output, i.e. *why* the agent made
   the change.
5. The fix keeps the intent (e.g. still saves tokens) and verification shows the trace
   metrics back at baseline. Show one rejected attempt and the retry.
6. `agent-bisect report`: before/after table and the run's own cost, which feeds the
   cfo.ai unit economics (cost per resolved regression).
