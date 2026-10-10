# T2: Unit economics from measured runs, as input for cfo.ai

**Goal:** turn what Agent Medic actually spent into a cost-per-incident model, and write the
brief the founder pastes into cfo.ai (an AI CFO, "Ari") to build the business plan: pricing,
costs, revenue, margin, runway. Every number must trace back to a file or be an explicit,
listed assumption.

**You own:** `scripts/unit_economics.py`, `docs/business/` (new files),
`tests/test_unit_economics.py`. Task name for the PR branch: `unit-economics`.

## Part 1: `scripts/unit_economics.py`

`python scripts/unit_economics.py [--medic-dir .medic]` reads the runtime data (formats in the
shared rules) and writes:

- `docs/business/measured.csv`: one row per resolved or escalated incident, from
  `incidents/*.json`, every `bench/*/incidents/*.json`, and `external/case-*.json`. Columns:
  `source, incident, scenario_or_target, cause, outcome, detect_s, resolve_s, fix_attempts,
  fixer_input_tokens, fixer_output_tokens, eval_runs, eval_cases, eval_input_tokens,
  eval_output_tokens, neatlogs_api_calls_estimate`.
  - Fixer tokens: sum of `attempts[].usage` (`remedy.attempts` for incidents, `attempts` for
    the external case).
  - Eval tokens: eval result files Medic ran for that incident or bench run: each bench dir's
    `deploy/.bisect/results/*.json`, and the shared `.medic/deploy/.bisect/results/*.json` by
    incident id where the file name contains it (`verify-<incident>-N`, `attempt-...`). Sum
    `input_tokens` and `output_tokens` over `cases`. If a file can't be attributed to an
    incident, attribute it to its bench run and say so.
  - neatlogs API calls: estimate per incident as `traces read × (1 span list + tool spans + 1
    agent payload)`, using the incident's `trace_ids` and the matching trace evidence in
    `state.json`. Document the formula.
- `docs/business/unit_economics.md`, a readable report:
  1. **Measured per incident:** median and p90 of fixer tokens, eval tokens, time to detect,
     time to resolve, attempts, broken down by cause.
  2. **Cost per incident at three price points**, from a `PRICES` table at the top of the
     script (USD per million tokens, input/output), each with a source or "assumption":
     - *As run:* Ollama Cloud `gpt-oss:120b` (fixer) and `gpt-oss:20b` (agent and evals); free
       tier today, so price them at a comparable hosted open-model per-token rate and name it.
     - *Claude mix:* Claude Haiku 5.5 for evals ($0.10 in / $0.50 out) and Claude Opus 5.5 for
       the fixer ($4 / $20).
     - *All Opus:* Claude Opus 5.5 for everything.
  3. **Monitoring overhead per watched agent per month:** canary every 10 minutes (8 eval
     cases, tokens per case from the measured eval files) plus neatlogs reads, at each price
     point; interval as a parameter.
  4. **Infrastructure:** one EC2 `t3.small` in us-east-1, 24/7 (cite the on-demand price or
     mark ~$0.0208/h as an assumption).
  5. **Assumptions table:** every unmeasured number with a one-line justification.

Keep the math in small named functions. `tests/test_unit_economics.py` covers token
aggregation on a temp `MEDIC_DIR` with two fake incidents and one fake eval result.

## Part 2: `docs/business/cfo_brief.md`

A request to a CFO, with every number inline (pulled from Part 1's output):

- **Product:** Agent Medic, an on-call engineer for production AI agents. It watches neatlogs
  traces, diagnoses why an agent broke (bad release, provider retired a model, dependency API
  changed, runaway loop), ships a verified fix, confirms recovery, and escalates what code
  can't fix. Proof points: 6/6 benchmark scenarios, a real fix to neatlogs' own example.
- **Customer:** teams running LLM agents in production; beachhead: startups with 1-10 agents.
- **Pricing to model** (propose with reasoning and ask cfo.ai to stress-test):
  Free (1 agent, detection and diagnosis only), Team (per monitored agent per month, includes N
  healed incidents), overage per healed incident. Anchor against the cost of an engineer-hour
  on call and against Sentry Seer's public $40 per active contributor per month.
- **Costs:** variable cost per incident and per monitored agent per month (Part 1),
  infrastructure, observability and model API costs, founder salaries as an assumption.
- **Scenarios over 24 months:** base and growth: new customers per month, churn, agents per
  customer, incidents per agent per month, all explicit assumptions.
- **Ask:** build the P&L, gross margin, burn and runway; flag the riskiest assumptions; suggest
  price points that keep gross margin above 75%.

End with **"What to export back"**: the cfo.ai share link, a screenshot of the model, and
`docs/business/BUSINESS_PLAN.md`, a template the founder fills from cfo.ai's output with
headings: Summary, Pricing, Unit economics, Scenarios, Runway, Risks, Assumptions.

## Acceptance

- On the real `.medic/`, `measured.csv` has rows for the 6 benchmark incidents, the live
  incidents in `.medic/incidents/` and the external case.
- Every price is in one table with a source or "assumption".
- Full test suite passes. PR via the shared rules' worktree procedure.
