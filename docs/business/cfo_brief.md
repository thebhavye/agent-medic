# Brief for cfo.ai (Ari): business plan inputs for Agent Medic

Paste everything below into cfo.ai. Every dollar number comes from
`docs/business/unit_economics.md` (regenerate with
`python scripts/unit_economics.py --medic-dir .medic`); anything else is an
explicit assumption, marked as such.

## Product

Agent Medic is an on-call engineer for production AI agents. It watches neatlogs
traces, diagnoses why an agent broke (bad release, provider retired a model,
dependency API changed, runaway loop), ships a verified fix, confirms recovery,
and escalates what code can't fix. Proof points: 6/6 benchmark scenarios
resolved-or-correctly-escalated, plus a real fix to neatlogs' own example
(model `gemini-2.5-flash` retired -> `gemini-3.8-flash`).

## Measured unit economics (7 terminal incidents: 1 live, 5 bench, 1 external)

- Median per incident: 3,084 fixer tokens, 19,219 eval tokens, 130s to detect,
  378s (~6 min) to resolve, 1 fix attempt (p90: 14,801 / 146,066 tokens,
  34 min to resolve, 3 attempts).
- Variable cost per healed incident: $0.0060 as run (hosted open-weight rates,
  assumption), $0.0368 on Claude mix (Haiku evals, Opus fixer), $0.1845 all-Opus.
- Monitoring per watched agent per month (8-case canary every 10 min, 4,320
  runs): $8.77 as run, $10.73 Claude mix, $429.19 all-Opus.
- Infrastructure: one EC2 t3.small in us-east-1, 24/7 = $15.18/month
  ($0.0208/h, assumption).
- Blended variable cost per agent per month, Claude mix, at an assumed 2
  incidents/agent/month: about $10.80 ($10.73 canary + $0.07 incidents).

## Customer

Teams running LLM agents in production. Beachhead: startups with 1-10 agents
that have no on-call rotation for their agent stack.

## Pricing to model (stress-test these)

- Free: 1 agent, detection and diagnosis only (no auto-fix). Acquisition, not revenue.
- Team: $49 per monitored agent per month, includes 4 healed incidents.
  At Claude-mix costs (~$10.80/agent/month at 2 incidents) that is ~78% gross
  margin; overage at $9 per healed incident (cost ~$0.04) stays far below one
  engineer-hour on call (assumed $100/h fully loaded) and beside Sentry Seer's
  public $40 per active contributor per month.
- Ask Ari to stress-test: the incident allowance, the overage price, and an
  annual-discount Team tier.

## Other costs (assumptions for Ari to sharpen)

- Observability/model APIs beyond the above: assume $0 (neatlogs reads are
  negligible next to model tokens).
- Founder salaries: assume $0 cash until seed (sweat equity); flag as the first
  thing that breaks the runway model.

## Scenarios over 24 months (all assumptions, tune freely)

- Base: 2 new customers/month, 5% monthly logo churn, 3 agents/customer,
  2 incidents/agent/month.
- Growth: 5 new customers/month from month 7, 3% churn, 5 agents/customer,
  2 incidents/agent/month.

## Ask

Build the P&L, gross margin, burn and runway; flag the riskiest assumptions;
suggest price points that keep gross margin above 75%.

## What to export back

- The cfo.ai share link.
- A screenshot of the model.
- `docs/business/BUSINESS_PLAN.md`, filled from cfo.ai's output using the
  template already in this folder (headings: Summary, Pricing, Unit economics,
  Scenarios, Runway, Risks, Assumptions).
