# Agent Medic unit economics

Source: `.medic` - 12 terminal incidents (1 live, 10 bench, 1 external).
Raw rows: `measured.csv`.

## 1. Measured per incident

- Fixer tokens: median 3,110, p90 5,221.
- Eval tokens: median 30,058, p90 47,506.
- Time to detect (fault injection -> incident open; bench runs only): median 127s, p90 222s.
- Time to resolve from incident open: median 426s, p90 594s.
- Time to resolve from fault injection (bench runs only): median 518s, p90 700s.
- Fix attempts: median 1.0, p90 3.0.

| cause | n | median fixer tok | median eval tok | median detect | median resolve (open) | median resolve (fault) |
|---|---|---|---|---|---|---|
| code_regression | 7 | 3,137 | 44,260 | 141s | 465s | 664s |
| dependency_contract | 2 | 3,128 | 33,362 | 48s | 1,194s | 398s |
| dependency_data | 1 | 0 | 12,082 | 147s | 18s | 165s |
| dependency_outage | 1 | 0 | 8,724 | 113s | 74s | 187s |
| provider_model_retired | 1 | 14,801 | 0 | Unknown | Unknown | Unknown |

## 2. Cost per incident at three price points

Median incident: 2,252 fixer input + 722 fixer output tokens, 24,340 eval input + 5,718 eval output tokens. Each stream takes its median independently; the four medians need not come from one incident.

| price table (USD per M tokens) | input | output | source |
|---|---|---|---|
| as_run_fixer: gpt-oss:120b class (assumption) | 0.5 | 1.5 | assumption |
| as_run_eval: gpt-oss:20b class (assumption) | 0.1 | 0.3 | assumption |
| haiku: Claude Haiku 5.5 (task rate) | 0.1 | 0.5 | task rate |
| opus: Claude Opus 5.5 (task rate) | 4.0 | 20.0 | task rate |

| price point | per incident |
|---|---|
| As run | $0.0064 |
| Claude mix (Haiku evals, Opus fixer) | $0.0288 |
| All Opus | $0.2352 |

## 3. Monitoring overhead per watched agent per month

Canary every 10 minutes, 8 cases per run. Measured mean per eval case: 1,275 input + 243 output tokens.

| price point | canary runs/mo | canary cost/mo |
|---|---|---|
| As run | 4,320 | $6.93 |
| Claude mix (Haiku evals, Opus fixer) | 4,320 | $8.61 |
| All Opus | 4,320 | $344.22 |

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
| 7 | Live detection time is Unknown | only bench runs record fault-injection time; opening-to-triage lag is response time, not detection |
| 8 | Unattributed eval files are excluded | shared results matching no incident or investigated commit are listed below, counted nowhere |

## Eval attribution

- Bench rows sum every result file in that bench run's `deploy/.bisect/results/` (bisect + verify runs are all part of resolving it).
- Live verification sums shared `deploy/.bisect/results/` files whose name contains the incident id (remedy records `verify-<incident>-<n>`).
- Live investigation sums files whose stem matches a commit recorded in `findings.regression` (`good`, `bad`, `culprit`, `verdicts`), since the evaluator names investigation results by commit, not incident.
- neatlogs API estimate per incident: traces read x (1 span list + tool-call payloads + 1 agent payload), from the incident's `trace_ids` against live state and the on-disk evidence cache. A missing trace is Unknown (empty cell), never zero; an empty trace list is a genuine zero.
