# Agent Medic: business plan

Built with cfo.ai (Ari) from the measured inputs in `cfo_brief.md`, `unit_economics.md` and
`measured.csv`. Model: <https://cfo.ai/s/ENh-oT3rP35cvUvi>. Forecast period October 2026 to
September 2028, in USD. These are forecasts, not actuals. The customer count starts at zero,
and the placeholders are marked as such.

## Summary

Agent Medic keeps the AI features of production apps working. Coding agents ship many
plausible changes, and some of them quietly break an app's chatbot, AI search or support agent.
Medic watches the feature through neatlogs. When it breaks, Medic serves the last good version
within minutes, finds the commit that caused it, reads its intent from Entire, and delivers a
verified fix as a pull request. Then it confirms the heal on real traffic.

- **Evidence so far:** 11/11 benchmark runs across 7 fault scenarios, one third-party RAG app
  healed end to end, and 12 measured incidents. Ari rates this as evidence that the product is
  feasible. It is not yet evidence of retention, acquisition or production reliability.
- **The economics hinge on monitoring, not healing.** A healed incident costs about $0.03, but
  the known-answer checks cost $8.61 per agent per month on a Claude mix and $344 if every
  check ran on Opus. Model routing and check budgets are therefore part of the product, not
  an optimisation.
- **Pricing:** the launch price of $49 per agent clears the margin target on product costs
  alone (about 79%). It misses once support is counted (about 59%). Ari recommends testing
  **$99 per agent per month with a 10% annual discount**, plus fleet plans.
- **Runway:** a lean base case with founders unpaid reaches monthly operating breakeven in
  March 2027 and never runs out of cash over 24 months. Paying founders requires funding.

## Pricing

| Offer | Revenue per month | Fully loaded margin | Product-only margin |
|---|---|---|---|
| Team $49 monthly (3 agents) | $147 | 59.1% | 79.2% |
| Team $59 monthly (3 agents) | $177 | 65.5% | 82.7% |
| Team $49 at 10 incidents per agent ($9 overage on 6 each) | $309 | 78.8% | 89.9% |
| Team $49 annual, 10% off | $132 | 55.1% | 76.9% |
| Team $49 annual, 20% off | $118 | 49.8% | 74.0% |
| Fleet 10 agents · $599 | $599 | 75.9% | 83.0% |
| Fleet 25 agents · $1,399 | $1,399 | 77.1% | 81.8% |

"Fully loaded" includes support ($25 per customer per month) and card fees. "Product-only"
excludes both. The target is a fully loaded margin above 75%.

**Recommendation (Ari):**
- **Test $99 per agent per month with a 10% annual discount.** That is $89.10 a month
  equivalent, or $1,069.20 prepaid per agent per year. $89 a month only just clears 75% at
  efficient scale, and $99 leaves room. A 15% discount on $99 is about 75.1% in the ideal
  model and can slip below 75% when servers are bought whole. A 20% discount fails.
- **Keep 4 included healed incidents and a $9 overage.** At 2 incidents a month the allowance
  brings in no overage revenue. Raising it to 8 costs only about $0.12 per agent, so the
  allowance is a cheap marketing lever.
- **Fleet plans at $599 for 10 agents and $1,399 for 25,** with a capped check frequency and
  model routing. These are tests at full occupancy. Idle agents, more support or more frequent
  checks reduce their margin.
- **$49 works only with almost no paid support,** about 1 minute per customer per month. When
  founders do the support for free, cash burn is lower, but the true cost of support remains.

## Unit economics

Measured from 12 terminal incidents: 1 live, 10 benchmark, 1 external. Fixer tokens are real
model usage. Eval tokens in the benchmark runs are **estimated** (characters ÷ 4 of each
request), because the benchmark's target agent runs a deterministic scripted policy instead
of a model. The live incident's eval tokens are real. The monitoring cost per agent comes
mostly from those estimates, so it should be re-measured on a real model before pricing.

| Item | Value |
|---|---|
| Cost per healed incident (Claude mix: Haiku checks, Opus fixer) | $0.0288 |
| Cost per healed incident (all Opus) | $0.2352 |
| Monitoring per agent per month (Claude mix, check every 10 min) | $8.61 |
| Monitoring per agent per month (all Opus) | $344.22 |
| Infrastructure | $15.18 per t3.small instance per month, assumed 10 agents per instance |
| Support (assumption) | 15 minutes per customer per month at $100/h |
| Card fees | 2.9% + $0.30 per charge |
| Median time to detect / to resolve after the incident opens | 127 s / 426 s |

At $49 per agent, the monthly forecast shows a product-only gross margin of 77–79% and a total
gross margin of 57–59%, because support takes about 20 points.

## Scenarios

All scenarios cover 24 months from October 2026 and start with zero customers.

| | Base (Main) | Growth | Base · paid founder | Downside · paid founder |
|---|---|---|---|---|
| Year 1 revenue | $19,209 | $49,001 | $19,209 | $8,147 |
| Year 1 gross profit | $11,250 | $32,273 | $11,250 | $3,887 |
| Year 1 operating profit | $1,650 | $22,673 | −$28,350 | −$35,713 |
| Year 2 revenue | $42,812 | $175,797 | $42,812 | $14,959 |
| Year 2 gross profit | $25,217 | $115,923 | $25,217 | $7,231 |
| Year 2 operating profit | $15,617 | $106,323 | −$44,383 | −$62,369 |
| Cash at month 24 | $42,267 | $153,996 | −$47,733 | −$73,082 |
| Lowest cash | $23,400 | $24,360 | −$47,733 | −$73,082 |
| Funding needed to stay above zero | $0 | $0 | $47,733 | $73,082 |

- **Base:** 2 new customers a month, 5% monthly churn, 3 agents per customer, 2 incidents
  per agent per month, $49 per agent.
- **Growth:** 3% churn and 5 agents per customer. New customers rise from 2 to 5 a month
  in April 2027.
- **Paid founder:** adds $5,000 a month of founder pay from April 2027.
- **Downside:** Ari's combined downside with founder pay. Its revenue is about 40% of base
  in year 1 ($8.1k vs $19.2k).
- **Price cases:** the model also holds Annual $99 and Monthly $99 cases. They keep demand
  fixed, so they test cost, not willingness to pay.

## Runway

- **Opening cash is a $25,000 placeholder.** No fundraising is assumed.
- **The base case is lean.** It covers its monthly cash costs from March 2027 (the first month
  with positive operating profit is $108) and never runs out of cash in the 24 months. It
  bottoms out at $23,400. Ari's caveat: this does not show that the business can fund a
  salaried team.
- **Paying founders runs the cash out.** $5,000 a month in founder pay from April 2027
  exhausts the placeholder cash in September 2027. The combined downside exhausts it in July
  2027. A negative cash balance here measures the funding needed, not permission to spend.
- **Annual prepayments help cash but create a service obligation.** They are not all profit.

## Risks

Flagged by Ari:

- **Churn:** 5% monthly churn is about 46% of customers lost per year.
- **Acquisition:** 2 new customers a month is not validated, and the $300 a month spend
  assumption doesn't explain it.
- **Cost outliers:** 12 incidents don't price long contexts, repeated fixes or abusive tenants.
- **Coverage:** known-answer checks learned from the app's answers may miss changing
  knowledge and customer-specific failures.
- **Permissions:** automated PR and rollback permissions bring security and support
  obligations.
- **Capacity:** 10 agents per t3.small is an untested assumption.
- **Model choice:** monitoring on all-Opus defaults ($344 per agent per month) makes every
  offer uneconomic. Model routing, token and check budgets, and escalation limits must be
  enforced in the product.

Ours:

- **Dependence on neatlogs and Entire.** Detection and intent come from partner platforms.
  Their APIs are still moving; for example, alerts can't yet be scoped per workflow and
  detections can't skip tagged traces.
- **Python only today.** The runtime and runner cover Python agents. TypeScript backends,
  common for web apps, are not yet supported.

## Assumptions

| Assumption | Value | Status |
|---|---|---|
| Opening cash | $25,000 | placeholder, replace before use |
| Overhead | $500 per month | placeholder |
| Acquisition spend | $300 per month | placeholder, not causally linked to acquisitions |
| Support | 15 min per customer per month at $100/h | assumption, treated as cash cost |
| Founder salary | $0 (Main); $5,000 per month from April 2027 (paid-founder cases) | assumption |
| Agents per instance | 10 per t3.small; servers bought whole | assumption, untested |
| Model rates | Claude mix: Haiku checks, Opus fixer | task rates, not invoices |
| Eval tokens (benchmark) | characters ÷ 4 per request | estimate: the benchmark agent is scripted, not a model |
| Monitoring cadence | an 8-question check every 10 minutes | cost per check: mostly estimated tokens (scripted benchmark agent) |
| Incidents | 2 per agent per month | assumption |
| Self-hosted users | excluded from revenue and costs; no conversion rate assumed | by design |
| Not modelled | seed funding, sales tax or VAT, income tax, capex, debt, bad debt, refunds | operating profit is before these |
