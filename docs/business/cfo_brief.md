# Brief for cfo.ai (Ari): business plan inputs for Agent Medic

Paste everything below into cfo.ai. Every dollar number comes from
`docs/business/unit_economics.md` (regenerate with
`python scripts/unit_economics.py --medic-dir .medic`); anything else is an
explicit assumption, marked as such.

## Product

Agent Medic keeps AI features in production working. Developers build their apps with coding
agents (Claude Code, Cursor, Codex), which ship many plausible changes; some quietly break
the app's AI feature (a chatbot, AI search, a support agent). Nothing crashes: answers just
get worse, and ordinary monitoring doesn't notice.

Medic watches the AI feature through neatlogs traces and alerts, and when it breaks:

1. **First aid, in minutes:** serves the last known good version of the broken part (a prompt,
   a setting, a retrieval function), after proving on known-answer questions that it helps.
2. **The cure:** finds the commit that broke it, reads why it was made from its Entire
   checkpoint, writes a fix that keeps that intent, and verifies it.
3. **Delivery:** the fix arrives as a pull request with the evidence (opened by Medic, or
   prepared for the developer to open). After the merge is deployed, Medic confirms the heal
   on real traffic, removes first aid, and opens a revert PR if the fix didn't hold.

Onboarding is one command (`agent-medic init`): it reads the app's code and proposes one
reviewable diff. No tests to write: known-answer checks are learned from the app's own answers.

## Proof points (measured)

- Benchmark: 11/11 unattended runs passed across 7 fault scenarios (bad release, upstream API
  change, retry loop, truncated documents, outage and stale data correctly escalated to a
  human, healthy release with no false alarm).
- A real third-party app (an open-source LangChain RAG tutorial, unmodified apart from
  onboarding): a plausible commit ("only answer from strongly matching passages") silently
  turned answers into "Unable to find matching results". Medic detected it in 1 min 47 s,
  had first aid live at 7 min 23 s (proven 50% -> 100% on known-answer questions), and a
  verified fix PR ready at 11 min. After the merge was deployed it confirmed the heal on real
  traffic.
- Immunity: afterwards a coding agent (Claude Code) was asked for the same change again and
  refused, citing Medic's fix commit as the reason. The fix history teaches the next agent.
- A real fix to neatlogs' own example app (model `gemini-2.5-flash` retired -> `gemini-3.8-flash`).

## Measured unit economics (12 terminal incidents: 1 live, 10 benchmark, 1 external)

Fixer tokens are real model usage. Benchmark eval tokens are estimated (characters ÷ 4 per
request) because the benchmark's target agent is a deterministic scripted policy; the live
incident's are real. Treat the monitoring cost as an estimate until re-measured on a model.

Re-measured since on the real RAG app: 299 input + 190 output tokens per known-answer
question, so $3.24 per agent per month on Claude Haiku at a 10-minute check (vs the $8.61
estimate). Tool-using agents cost more per question; measure per customer.

- Median per incident: 3,110 fixer tokens, 30,058 eval tokens, 127 s to detect (fault
  injection -> incident open; benchmark runs only), 426 s (~7 min) to resolve from incident
  open, 1 fix attempt (p90: 5,221 / 47,506 tokens, 594 s to resolve, 3 attempts).
- Variable cost per healed incident: $0.0064 as run (hosted open-weight rates, assumption),
  $0.0288 on a Claude mix (Haiku for checks, Opus for the fixer), $0.2352 all-Opus.
- Monitoring per watched agent per month (8-question check every 10 min, 4,320 runs):
  $6.93 as run, $8.61 Claude mix, $344.22 all-Opus.
- Infrastructure: one EC2 t3.small in us-east-1, 24/7 = $15.18/month ($0.0208/h, assumption).
  Assumption: one such instance serves ~10 watched agents, so ~$1.52 per agent per month.
- Blended variable cost per agent per month, Claude mix, at an assumed 2 incidents per agent
  per month: about $10.19 ($8.61 checks + $0.06 incidents + $1.52 infrastructure).

## Customer

Teams that ship an AI feature to users and build with coding agents. Beachhead: startups and
small teams with 1-10 AI agents or features in production and no on-call rotation for them.

## Business model to model (stress-test these)

- **Open source, self-hosted (free):** the CLI and runtime; the team runs Medic on its own
  machine with its own model keys. Adoption and trust, not revenue.
- **Hosted Team: $49 per watched agent per month**, includes 4 healed incidents. Medic runs for
  them (no server, no keys to manage), with the dashboard and PR delivery. At ~$10.19 blended
  cost that is ~79% gross margin. Overage: $9 per healed incident (cost ~$0.03), far below one
  engineer-hour on call (assumed $100/h fully loaded) and beside Sentry Seer's public $40 per
  active contributor per month.
- Ask Ari to stress-test: the incident allowance, the overage price, an annual-discount Team
  tier, and whether a per-agent price fits teams with many small agents.

## Other costs (assumptions for Ari to sharpen)

- Observability and model APIs beyond the above: assume $0 (neatlogs reads are negligible next
  to model tokens).
- Founder salaries: assume $0 cash until seed (sweat equity); flag as the first thing that
  breaks the runway model.

## Scenarios over 24 months (all assumptions, tune freely)

- Base: 2 new paying customers per month, 5% monthly logo churn, 3 agents per customer,
  2 incidents per agent per month.
- Growth: 5 new paying customers per month from month 7, 3% churn, 5 agents per customer,
  2 incidents per agent per month.

## Ask

Build the P&L, gross margin, burn and runway; flag the riskiest assumptions; suggest price
points that keep gross margin above 75%.

## What to export back

- The cfo.ai share link.
- A screenshot of the model.
- `docs/business/BUSINESS_PLAN.md`, filled from cfo.ai's output using the template already in
  this folder (headings: Summary, Pricing, Unit economics, Scenarios, Runway, Risks, Assumptions).
