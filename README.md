# Nexus: An API to call from subscription based cancellations

**Connect Sentry, Datadog, Mixpanel and Stripe, call one API from your cancel flow, and Nexus tells you whether you
owe that customer a remediation.**

**Demo video:** [Watch the demo](https://youtu.be/i1OanAMc770)

## What it does

When a customer clicks cancel, Nexus asks one question: **did this customer actually have a bad time because of
us, and can we prove it?**

It looks at three systems for that customer in the weeks before they cancelled (Sentry for the errors they hit,
Datadog for the confirmed outages in their region, Mixpanel for whether the broken feature is something their team
depends on) and decides whether there is a real, causal story.

If there is: **remediate**. Own the failure, show the evidence, offer a remedy (a Stripe credit).

If there isn't: proceed with the business's standard retention offer (configurable, e.g. a
discount), exactly as the normal cancel flow would.

That second branch is the point of the project. Handing out discounts is trivial; a system that refuses to invent a
grievance is not. Both paths get demoed, and every failure path ends in the standard offer, never a false
remediation.

This is technical churn only. Someone who simply didn't like the product is not a bug.

## What each app tells Nexus

| App | The question only it can answer | For Fathom |
| --- | --- | --- |
| **Sentry** | Did *this customer* hit the failure? | 31 failed syncs on their own account on Sep 3 |
| **Datadog** | Was it a *confirmed outage* where they are? | "Sync jobs timing out", declared in us-east-1, Sep 3 |
| **Mixpanel** | Did it break something they *depend on*? | Data syncing is 82% of their team's activity; 4 of 6 teammates were working |
| **Stripe** | What's the relationship, and how do we make it right? | Pro plan; $60 credit applied when they click |

Sentry and Datadog only ever see failures. Mixpanel is the only one that sees normal work, so it's what tells Nexus
whether a failure actually mattered to this customer.

## Hackathon requirements

| Requirement | How Nexus meets it | Where to see it |
| --- | --- | --- |
| **Build one useful, multi-step AI agent** | One Claude Sonnet 5 agent in a tool loop chooses which evidence to fetch, runs a required self-check, and submits a typed verdict | Demo step trace; `backend/src/nexus/agent/loop.py` |
| **Connect it to at least three external apps** | Four live apps: Sentry, Datadog, Mixpanel (agent read tools) and Stripe (account context and remediation actions) | Demo tiles; seeded data in each app's dashboard |
| **Show how you know it works** | Code checks on every verdict; offline test suite; evals with 0 false remediations; `nexus verify` re-reads citations from the live apps; live Stripe safeguard tests | [`evals/results/latest.md`](evals/results/latest.md), [`examples/`](examples), [`docs/system-brief.md`](docs/system-brief.md) |

## Start here

| I want to… | Do this |
| --- | --- |
| See the demo (no keys, no network) | `cd backend && uv sync && uv run nexus serve`, open http://127.0.0.1:8765 and click **Cancel subscription**. It replays recorded real runs. |
| See the agent think in the terminal (no keys) | `uv run nexus run fathom --fake`, then `uv run nexus run quanta --fake` |
| Read a real run | [`examples/`](examples): Claude Sonnet 5 on live data, with verification |
| See the evals | [`evals/results/latest.md`](evals/results/latest.md) |
| Understand the design | [`docs/system-brief.md`](docs/system-brief.md) and [`docs/stack.md`](docs/stack.md) |

## Architecture

```
cancel clicked (customer_id, cancel time)
        │
   ┌────▼─────┐
   │  STRIPE  │  load the account: plan, price, region, prior credits
   └────┬─────┘
        │
   ┌────▼───────────────────────────┐
   │  NEXUS AGENT (Claude Sonnet 5) │  one agent, ≤10 turns, read-only tools,
   │  chooses the next tool calls   │  picks what to look at next each turn
   └──┬──────────┬──────────┬───────┘
      ▼          ▼          ▼
   Sentry     Datadog    Mixpanel       <- plain API calls. NOT agents.
   errors     outages    feature use,
   this       in their   who was
   customer   region     working
      └──────────┼──────────┘
                 ▼  evidence board (every result gets a stable ID)
       try_to_disprove → submit_verdict
                 │
   ┌─────────────▼─────────────┐
   │  FIVE CHECKS (code)       │  recomputed from the raw evidence, stop at the first failure
   └─────────────┬─────────────┘     1 every claim points to real data
          ┌──────┴──────┐            2 this customer hit the failure (Sentry)
     any  │             │ all        3 it was a confirmed outage in their region (Datadog)
     fail ▼             ▼ pass       4 it broke something they depend on (Mixpanel)
  STANDARD OFFER    REMEDIATION      5 the evidence is strong enough
                    what broke + proof it's fixed + Stripe credit
```

The customer's click then applies the Stripe credit, discount or scheduled cancel (test mode).

Five design decisions carry it:

- **The model proposes, code decides.** The agent gathers evidence and judges; the five checks recompute every
  fact from the raw tool results before anything reaches the customer.
- **Read-only agent.** The agent has no write tools. Stripe writes happen in code, only on a customer click, and a
  credit requires a recorded run that proved harm for that exact incident.
- **Our own loop.** About 100 lines on the Anthropic SDK, so each guard is explicit and tested: no verdict before the
  usage check and self-check, no verdict in the same turn as other tools, a step budget, tool errors returned as
  data. See [`docs/stack.md`](docs/stack.md) for why not LangGraph, Pydantic AI or the Agent SDK.
- **Honest by default.** Offer copy that admits fault ("sorry", "outage"…) is rejected at config load. Customer text
  uses only numbers from cited evidence.
- **Every run is recorded.** The demo replays real runs for free and offline; `nexus verify` re-reads their
  citations from the live apps.

## Reliability brief

A summary, while a deep dive is in [`docs/system-brief.md`](docs/system-brief.md); the tech stack is documented in
[`docs/stack.md`](docs/stack.md).

**Core guarantee:** every failure path ends in the business's standard offer. None ends in a false remediation.

### Trust boundaries

| The model can | The model cannot |
| --- | --- |
| Choose which evidence to fetch, and in what order | Write to any app: it has no write tools |
| Propose a claim, confidence and reasoning | Decide the outcome: the five checks in code do |
| Cite evidence it fetched | Cite anything it didn't fetch (check 1 rejects it) |
| Say how confident it is | Clear the bar without the evidence also passing checks 2–4 |
| | Set or apply a credit: the amount comes from config, and a credit needs a saved run that proved harm plus a customer click |

### Failure modes

| Failure | What happens | Proof |
| --- | --- | --- |
| Agent invents evidence | Check 1 rejects any ID a tool didn't return → standard offer | `tests/test_gates.py` |
| Agent blames an incident in another region | Check 3 recomputes region and timing from Datadog data | `tests/test_gates.py` |
| Agent is overconfident on a weak story | Checks 2–4 recompute from raw evidence; check 5 needs ≥0.75 | first real run at 0.72 was correctly refused |
| Customer hit errors in a feature they barely use | Check 4 needs the broken feature to be ≥50% of their activity, with someone working that day | `tests/test_gates.py` |
| Sentry, Datadog or Mixpanel down, slow or rate-limited | One retry after a short pause, then the error goes to the agent as data; missing evidence → standard offer | `tests/test_http.py`, `tests/test_pipeline.py` |
| Claude errors or runs out of steps | "Last chance" nudge, then `insufficient_evidence` → standard offer | `tests/test_loop.py` |
| Agent submits before checking usage or trying to disprove itself | Verdict refused; the agent must keep investigating | `tests/test_loop.py` |
| Prompt injection inside app data | Tool output is treated as data, and the checks decide regardless | checks recompute from raw evidence |
| Offer copy that admits fault ("sorry", "outage"…) | Config refuses to load | `tests/test_pipeline.py` |
| Double click on a credit | "Already credited" check plus a Stripe idempotency key | live Stripe test, `tests/test_stripe_actions.py` |
| Credit requested without proof | API returns 409 | `tests/test_stripe_actions.py` |
| Saved evidence no longer matches reality | `nexus verify` re-reads every citation from the live apps | [`examples/*/verify.txt`](examples) |
| No internet during the demo | Saved real runs replay with the same pacing | `tests/test_api.py` |

### How we know it works

- **Evals, Claude Sonnet 5 on 4 labelled scenarios:** 4/4 correct, **0 false remediations**, 0 missed
  ([results](evals/results/latest.md)).
- **Real runs on live data:** Fathom → remediation (7 steps, confidence 0.90, all five checks pass). Quanta →
  standard offer (4 steps, `no_harm`, 0.95) ([`examples/`](examples)).
- **Live verification:** `nexus verify` re-read every cited ID from Sentry, Datadog and Mixpanel: Fathom 3/3,
  Quanta 1/1.
- **Live Stripe checks (test mode):** credit applied once, duplicate refused, unproven incident refused, discount and
  scheduled cancel applied, reset reverses all of it.
- **Offline tests:** 65 pytest tests with fixtures and a scripted model, no network or keys. CI runs lint, the tests
  and both offline scenarios on every pull request.

### Trade-offs

| Decision | Gain | Cost |
| --- | --- | --- |
| One agent, not multi-agent | Simple to test and explain; one trace | No separate agent arguing the other side (the self-check and code checks cover it) |
| Our own ~100-line loop, not a framework | Every guard is explicit and tested | We maintain it; no built-in tracing |
| Code checks decide, the model proposes | Deterministic and auditable | Can miss real harm that doesn't fit the rules |
| Standard offer as the fallback | Every customer still gets the normal retention flow | Hurt customers the checks miss get no remediation |
| Claude Sonnet 5, not Opus 5 | Faster and cheaper per cancellation | Less headroom on subtle cases; needed prompt calibration |
| Fixed thresholds (10 errors, within a day, 50% share, 0.75) | Transparent and testable | Not tuned per business; adjustable in config |
| Direct REST, not vendor MCP servers | Exact queries, stable IDs, controlled retries | More client code to maintain |
| Replays in the demo | Free rehearsals, identical runs, works offline | A replay isn't a fresh run (`?fresh` runs live) |

### Security

- API keys live only in `.env`, which git ignores; each key has only the permissions it needs, and Stripe is test mode.
- `nexus serve` listens on this machine only (`127.0.0.1`); the API has no login, so it isn't meant to be deployed.
- Stripe customer IDs are left out of the event stream and saved runs.

### Known limits

- The Datadog trial can't store incident impact windows via the API, so the window is written into the incident title.
- Mixpanel's free plan blocks the query API; Nexus reads the export API instead.
- Evals are small: 4 labelled scenarios plus 2 live customers.
- No personal-data redaction before tool output reaches the model.
- One local server: no auth, multi-tenancy or deployment; a real product would call Nexus from its cancel webhook.

## Run it live

Needs accounts for Anthropic, Sentry, Datadog, Mixpanel and Stripe (test mode). Copy `.env.example` to `.env` and
fill in the keys (details in [`docs/system-brief.md`](docs/system-brief.md#7-operations)).

```bash
cd backend
uv sync
uv run nexus seed                    # dry run: shows the demo data it would create
uv run nexus seed --send             # create it in Sentry, Datadog, Mixpanel and Stripe
uv run nexus run fathom --live       # real agent on live data
uv run nexus serve                   # demo at http://127.0.0.1:8765 (add ?fresh for a new live run)
uv run nexus verify ../replays/fathom.live.claude.jsonl
uv run nexus eval                    # grade all scenarios with Claude
uv run nexus stripe-reset            # undo demo clicks in Stripe
```

The standard offer and remediation credit are configured per business in
[`backend/config/business.toml`](backend/config/business.toml).

## Repo layout

```
backend/src/nexus/
  agent/     loop, tools, evidence board, Claude and scripted models, prompt
  decide/    the five checks, remediation and standard offer
  apps/      live Sentry/Datadog/Mixpanel/Stripe clients, shared HTTP helper, fixtures, Stripe actions
  seed/      demo data for the live apps
  api/       streaming API, replays, customer actions
  pipeline.py  cli.py  evals.py  verify.py
demo/v2/     the demo page and per-app evidence pages
fixtures/    scenario data · examples/  captured real runs · evals/  results
docs/        system and reliability brief, stack rationale
```

Built for a hackathon.
