# agent-bisect run `bdb6ecb4`

Range: `18afc185a4` (good) .. `6db81ecb03` (bad). 3 regression(s) investigated, 3 fixed and verified.
Fix branch: `agent-bisect/fix-bdb6ecb4` at `e7d0f02abe`.

## Before and after

| Version | Pass rate | Error rate | Cost/run | Tokens/run | Tool calls/run |
|---|---|---|---|---|---|
| Baseline `18afc185` | 100% | 0% | $0.0235 | 4850 | 3.5 |
| Bad `6db81ecb` | 0% | 100% | $0.0535 | 9850 | 6.5 |
| Fixed `e7d0f02a` | 100% | 0% | $0.0235 | 4850 | 3.5 |

Metrics source: simulated. Cost of this run: $7.60 on evals, $0.17 on the fixer, 20 evaluations.

## 1. errors regression: fixed and verified

`error_rate`: 0 (baseline) -> 1 (bad).

**First bad commit:** `f234c5b4c4` Shorten tool parameter names to save prompt tokens

Bisection steps:

- `1ca6864b` Document read_doc: **good**
- `7778bdf9` Rename leadership doc title: **bad**
- `f234c5b4` Shorten tool parameter names to save prompt tokens: **bad**

Why it was made, from the commit message (no Entire session found):

> Shorten tool parameter names to save prompt tokens
>
> Tool schemas are sent on every turn; 'q' is shorter than 'query'.

**Attempt 1:** accepted. verified: error_rate 1 -> 0 (baseline 0)

Diagnosis: The schema advertises `q` but the handler still reads args['query'], so every search_docs call raises KeyError and the agent never sees a document.

Intent kept: The saving is a single token per turn and changing the handler too would break existing callers, so the schema goes back to `query`.

Fix commit: `ec5cc2b812`

## 2. quality regression: fixed and verified

`pass_rate`: 1 (baseline) -> 0.25 (bad).

**First bad commit:** `85cec6a26d` Cap read_doc output to keep the context window small

Bisection steps:

- `1ca6864b` Document read_doc: **good**
- `7778bdf9` Rename leadership doc title: **good**
- `bb811b42` Docstring for run_agent: **good**
- `85cec6a2` Cap read_doc output to keep the context window small: **bad**

Why it was made, from the commit message (no Entire session found):

> Cap read_doc output to keep the context window small
>
> Long documents were inflating input tokens on every later turn.

**Attempt 1:** accepted. verified: pass_rate 0.25 -> 1 (baseline 1)

Diagnosis: read_doc now cuts every document at 300 characters, before the facts the questions ask about, so answers miss names and figures.

Intent kept: Documents here are under 700 characters; the cap removes the content the tool exists to return, so read_doc returns the full body again.

Fix commit: `7c9e2fdde8`

## 3. cost regression: fixed and verified

`cost_per_run`: 0.0235 (baseline) -> 0.0535 (bad).

**First bad commit:** `f03561a0d5` Make the research agent double-check facts before answering

Bisection steps:

- `1ca6864b` Document read_doc: **bad**
- `54c9c809` Ignore 'about' in search queries: **good**
- `f03561a0` Make the research agent double-check facts before answering: **bad**

Why it was made, from the commit message (no Entire session found):

> Make the research agent double-check facts before answering
>
> Answers occasionally cited the wrong figure. Have the agent confirm each fact against its source before it answers.

**Attempt 1:** rejected. cost_per_run is 0.0535, still closer to the bad value 0.0535 than the baseline 0.0235

Diagnosis: Runs take too many turns; capping turns will bound the cost.

Intent kept: The verification instruction stays; the turn cap bounds it.

**Attempt 2:** accepted. verified: cost_per_run 0.0535 -> 0.0235 (baseline 0.0235)

Diagnosis: The prompt tells the agent to search again and re-read every source for each fact, which doubles tool calls and input tokens on every question.

Intent kept: The agent still has to read the full source before answering, which is what grounds the figure; only the redundant second search is removed.

Fix commit: `e7d0f02abe`
