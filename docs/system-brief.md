# Nexus: System and Reliability Brief

## 1. What it does

When a Mergeline customer clicks **Cancel subscription**, one Claude Sonnet 5 agent investigates three live apps
(Sentry for errors, Datadog for incidents, Mixpanel for feature usage) and proposes a verdict. Code re-checks that verdict
against the raw evidence.

- **Proven harm → remediation:** the customer sees what went wrong, that it's fixed, and gets a Stripe credit.
- **Anything else → the business's standard offer** (configurable, e.g. 20% off), with no admission of fault.

**Core guarantee:** every failure path ends in the standard offer. None ends in a false remediation.

### What each app is for

Each app answers one question the others can't. A remediation needs all three answers to be yes.

| App | The question it answers | Why the others can't | Nexus reads | Check |
| --- | --- | --- | --- | --- |
| **Sentry** | **Did this customer hit the failure?** | Datadog sees systems, not individual customers; Mixpanel doesn't record errors | Errors tagged with the customer's user ID: counts per day, peak day, fix release | 2 |
| **Datadog** | **Was it a confirmed outage where they are?** | Sentry sees scattered errors, not whether engineering declared an incident, where, or for how long | Declared incidents: title, region, start and end, severity | 3 |
| **Mixpanel** | **Did it break something they depend on?** | Sentry and Datadog only see failures. Only Mixpanel sees normal work: which features the team uses and who was working | Each feature's share of the team's activity; teammates active on the day it broke | 4 |
| **Stripe** | **What's the relationship, and how do we make it right?** | Nobody else holds the plan, price or money | Plan, price, prior credits; writes the credit, discount or scheduled cancel on click | – |

**Why Sentry and Mixpanel don't overlap:** Sentry counts *failures* for a customer ("31 failed syncs"). It can't say
whether sync is the heart of their work or a feature they touch once a month, because it never sees successful use.
Mixpanel counts *normal use* ("sync is 82% of what this team does"). The same 31 errors mean remediation for a team
that lives in sync, and nothing for a team where it's a side feature.

## 2. Architecture

```
Demo page (demo/v2)
   │  GET /api/runs/stream  (Server-Sent Events)        POST /api/actions (on click)
   ▼                                                       │
FastAPI (api/main.py) ── saves every run to replays/       │
   ▼                                                       ▼
pipeline.run()                                    StripeActions (credit · discount · cancel)
   │ 1. load account ─────────────── Stripe (read)
   │ 2. agent loop (≤10 turns) ───── Sentry · Datadog · Mixpanel (read-only tools)
   │ 3. five checks (code) ───────── recomputed from the evidence the tools returned
   ▼
remediation  or  standard offer
```

## 3. Components

| Part | Responsibility | Where |
| --- | --- | --- |
| Agent loop | Calls Claude, runs requested tools, enforces loop rules, returns a typed `Verdict` | `backend/src/nexus/agent/loop.py` |
| Tools | 6 read-only tools plus `submit_verdict`; each result is added to an evidence board with a stable ID | `agent/tools.py`, `agent/board.py` |
| App clients | Live Sentry, Datadog, Mixpanel, Stripe; scenario fixtures with the same interface | `apps/live.py`, `apps/fixtures.py` |
| Checks | 5 checks, stop at the first failure | `decide/gates.py` |
| Outcome builders | Remediation text from cited evidence only; standard offer from config | `decide/remediation.py`, `backend/config/business.toml` |
| API | Streams runs, replays saved runs, applies Stripe actions | `api/main.py` |
| Stripe actions | Credit, discount, scheduled cancel, reset; idempotent | `apps/stripe_actions.py` |
| Seeding | Demo data in all four apps, dated relative to today | `seed/` |
| Scripted model | Deterministic stand-in for Claude, used by tests and offline runs | `agent/fake.py` |

## 4. Trust boundaries

| The model can | The model cannot |
| --- | --- |
| Choose which evidence to fetch, and in what order | Write to any app (it has no write tools) |
| Propose a claim, confidence and reasoning | Decide the outcome: code checks do |
| Cite evidence it fetched | Cite anything it didn't fetch (check 1 rejects it) |
| Say how confident it is | Clear the bar without the evidence also passing checks 2–4 |
| | Set the credit amount (config) or apply it (only a proven recorded run plus a customer click) |

**The five checks** (all must pass, and the agent must claim `causal_harm`):
1. **Every claim points to real data:** each cited ID was returned by a tool in this run.
2. **This customer hit the failure (Sentry):** ≥10 errors on their own account inside the window.
3. **It was a confirmed outage in their region (Datadog):** a declared incident in the customer's region, within a
   day of their errors.
4. **It broke something they depend on (Mixpanel):** the broken feature is ≥50% of the team's activity, and at least
   one teammate was working that day.
5. **The evidence is strong enough:** agent confidence ≥0.75.

## 5. Failure modes

| Failure | What happens | Outcome |
| --- | --- | --- |
| Sentry, Datadog or Mixpanel down, slow or rate-limited | One retry after a short pause (the app's Retry-After, capped at 5s); then the error goes back to the agent as a tool result | Missing evidence → standard offer |
| Claude API error | Loop stops with `insufficient_evidence` | Standard offer |
| Agent never submits (step budget) | "Last chance" nudge, then `insufficient_evidence` | Standard offer |
| Agent submits too early | Refused until usage was fetched and the self-check ran; refused in the same turn as other tools | Agent continues |
| Agent invents an ID or blames the wrong incident | Check 1 or 3 fails | Standard offer |
| Customer hit errors in a feature they barely use | Check 4 fails (feature share below 50%) | Standard offer |
| Agent overconfident on a weak story | Checks 2–4 recompute from raw evidence | Standard offer |
| Prompt injection inside app data | Prompt treats tool output as data; checks decide regardless | Can't force a remediation |
| Offer copy in config admits fault ("sorry", "outage"…) | Config refuses to load | Caught before any customer sees it |
| Double click on "Keep with credit" | "Already credited" check plus a Stripe idempotency key | One credit |
| Credit requested for an incident no run proved | API returns 409 | No credit |
| Unknown customer or broken fixture | `run.error` event | Standard offer |
| No internet during the demo | Recorded real runs replay with the same pacing | Demo still works |

## 6. How we know it works

- **Offline tests (pytest, no network or keys):** loop rules, all five checks including invented citations, apps
  down, remediation text using only evidence numbers, fault-word rejection in offer copy, the streaming API and
  replay, Stripe safeguards (proof required, configured amount, reversal logic), live-response parsing.
- **Real agent runs on live data (Claude Sonnet 5):**
  - **Fathom:** 7 steps across Sentry, Datadog and Mixpanel (Stripe loaded first), `causal_harm` at 0.90, all 5
    checks pass → remediation.
  - **Quanta:** 4 steps, `no_harm` at 0.95 → standard offer.
- **Evals (`evals/results/latest.md`):** Claude Sonnet 5 on all 4 fixture scenarios: **4/4 correct, 0 false
  remediations, 0 missed remediations**.
- **Live evidence verification (`nexus verify`):** every ID the recorded runs cited was re-read from Sentry, Datadog
  and Mixpanel and matched (Fathom 3/3, Quanta 1/1).
- **Live Stripe tests (test mode):** credit applied once, duplicate refused, unproven incident refused, discount
  and scheduled cancel applied, reset reverses all of it.
- **Calibration finding:** the first real Fathom run proposed the right claim at 0.72 and the checks correctly
  withheld remediation. A confidence scale in the prompt fixed it without lowering the bar.

## 7. Operations

| Task | Command (from `backend/`) |
| --- | --- |
| Seed demo data into all four apps (dry run by default) | `uv run nexus seed [--app sentry\|datadog\|mixpanel\|stripe] [--send]` |
| Run one cancellation in the terminal | `uv run nexus run fathom [--live] [--fake]` |
| Serve the demo | `uv run nexus serve` → http://127.0.0.1:8765 (`?fresh` for a new live run, `?agent=fake` for no Claude) |
| Undo demo clicks in Stripe | `uv run nexus stripe-reset` |
| Lint and test | `uv run ruff check src tests && uv run pytest` |

- **Secrets:** only in `.env`, which git ignores. Each key has only the permissions it needs (Sentry: project read,
  issue read/write, release admin; Datadog: incident read/write; Stripe: test mode).
- **Cost control:** saved runs replay for free; Claude is called only with `?fresh`, the CLI without `--fake`, or a
  customer with no saved run.

## 8. Trade-offs

| Decision | Pros | Cons | Why we chose it |
| --- | --- | --- | --- |
| **One agent, not multi-agent** | Simpler to reason about, test and explain; one trace | No separate agent to argue the other side | Matches the brief; the self-check step and code checks cover the skeptic role |
| **Our own ~100-line loop, not LangGraph / Pydantic AI / Agent SDK** | Loop rules are explicit, tested and easy to show | We maintain it; no built-in tracing or provider switching | Guards are part of the product |
| **Code checks decide, model proposes** | Deterministic, auditable, safe against model mistakes | Can miss real harm that doesn't fit the rules (false negatives) | A missed remediation costs a discount; a false one costs trust |
| **Standard offer as the fallback** | Every customer still gets the business's normal retention flow | Hurt customers the checks miss get no remediation | Honest by default; thresholds are config |
| **Claude Sonnet 5, not Opus 5** | Faster and cheaper per cancellation | Less headroom on subtle cases; needed prompt calibration | The checks limit the downside of a weaker judgment |
| **Fixed thresholds** (10 errors, outage within a day, 50% feature share, 0.75) | Transparent, testable | Not tuned per business or customer size | Easy to adjust in config; honest for a first version |
| **Direct REST, not vendor MCP servers** | Exact queries, stable IDs, controllable timeouts | More client code; we track API changes ourselves | Sentry's MCP search runs its own LLM; Datadog's MCP needs a person's OAuth |
| **Recorded replays in the demo** | Free rehearsals, identical runs, works offline | A replay isn't a fresh run (the footer says so) | Live demo reliability; `?fresh` still runs live |
| **Plain HTML demo, no React** | Zero build step; the approved design is the product UI | Harder to grow into a full app | One-night build; the UX was already approved |
| **Seeded demo data in real apps** | Real API calls, real IDs, real dashboards | Trial-plan workarounds (below); not production traffic | Proves the connections without a production customer |
| **Credit tied to a recorded proven run** | A click can't mint a credit without proof | Needs a run before the action | Money moves only on proof plus consent |

## 9. Known limits

- **Datadog trial:** the API can create incidents but can't store or edit the outage window fields, so the window
  is written into the incident title and parsed back.
- **Mixpanel free plan:** the query API is blocked, so Nexus reads the raw export API and computes each feature's share and the active teammates itself.
- **Feature mapping is configuration:** which product feature an error type or incident belongs to is set in
  `backend/config/business.toml`, not inferred.
- **Evaluation depth:** 4 fixture scenarios and 2 live customers, not a large eval set.
- **No personal-data redaction** before tool output reaches the model. IDs and counts only here, but not enforced.
  Stripe customer IDs are left out of the event stream and saved runs.
- **Single local server:** no auth, no multi-tenant config, no deployment.
- **Trigger:** the demo button starts a run; a real product would call Nexus from its billing or cancel webhook.
