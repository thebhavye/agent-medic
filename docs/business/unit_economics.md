# Agent Medic unit economics

Source: `.medic` - 7 terminal incidents (1 live, 5 bench, 1 external).
Raw rows: `measured.csv`.

## 1. Measured per incident

- Fixer tokens: median 3,084, p90 14,801.
- Eval tokens: median 19,219, p90 146,066.
- Time to detect: median 130s, p90 344s.
- Time to resolve: median 378s, p90 2,038s.
- Fix attempts: median 1.0, p90 3.0.

| cause | n | median fixer tok | median eval tok | median detect | median resolve |
|---|---|---|---|---|---|
| code_regression | 2 | 3,804 | 96,056 | 220s | 511s |
| dependency_contract | 2 | 3,128 | 22,706 | 160s | 1,218s |
| dependency_data | 1 | 0 | 12,082 | 147s | 165s |
| dependency_outage | 1 | 0 | 8,724 | 113s | 187s |
| provider_model_retired | 1 | 14,801 | 0 | 0s | 0s |

## 2. Cost per incident at three price points

Median incident: 3,084 fixer tokens, 19,219 eval tokens. Fixer/output split follows the median row; eval tokens use the median eval input/output split.

| price table (USD per M tokens) | input | output | source |
|---|---|---|---|
| as_run_fixer: gpt-oss:120b class (assumption) | 0.5 | 1.5 | assumption |
| as_run_eval: gpt-oss:20b class (assumption) | 0.1 | 0.3 | assumption |
| haiku: Claude Haiku 5.5 (task rate) | 0.1 | 0.5 | task rate |
| opus: Claude Opus 5.5 (task rate) | 4.0 | 20.0 | task rate |

| price point | per incident |
|---|---|
| As run | $0.0060 |
| Claude mix (Haiku evals, Opus fixer) | $0.0368 |
| All Opus | $0.1845 |

## 3. Monitoring overhead per watched agent per month

Canary every 10 minutes, 8 cases per run. Measured mean per eval case: 1,685 input + 284 output tokens.

| price point | canary runs/mo | canary cost/mo |
|---|---|---|
| As run | 4,320 | $8.77 |
| Claude mix (Haiku evals, Opus fixer) | 4,320 | $10.73 |
| All Opus | 4,320 | $429.19 |

neatlogs reads for monitoring are not in the token math above; the watcher polls the trace list continuously (assumption: negligible API cost next to model tokens).

## 4. Infrastructure

One EC2 t3.small in us-east-1, 24/7: 0.0208/h x 730h = $15.18/month (hourly rate is an assumption).

## 5. Assumptions

| # | assumption | justification |
|---|---|---|
| 1 | Hosted open-model rates for the as-run point | Ollama Cloud is free-tier today; priced at comparable hosted 100B+/20B-class per-token rates |
| 2 | EC2 t3.small ~$0.0208/h | on-demand list price from memory; verify before signing |
| 3 | Canary covers the 8 eval cases | matches evals/cases.jsonl today |
| 4 | Watcher neatlogs polling cost ~$0 | trace-list reads are cheap next to model tokens |
| 5 | Escalated incidents cost the same to work | they consume diagnosis + attempts before escalating |
| 6 | External case has no .bisect eval files | it runs its own verification harness |

## Eval attribution

- Bench rows sum every result file in that bench run's `deploy/.bisect/results/` (bisect + verify runs are all part of resolving it).
- Live rows sum shared `deploy/.bisect/results/` files whose name contains the incident id.
- neatlogs API estimate per incident: traces read x (1 span list + tool-call payloads + 1 agent payload), from the incident's `trace_ids` and matching evidence.
- Unattributed shared evals (not in any row): 0a2f14f0e87a.json (baseline runs).
