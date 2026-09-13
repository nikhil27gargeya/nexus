# Nexus: Build Plan

## 0. Hackathon requirements

| Requirement | Nexus |
| --- | --- |
| **One useful, multi-step AI agent** | One Nexus agent (Claude Sonnet 5) runs a tool loop. It decides which evidence to pull, in what order, when to try to disprove itself, and when to submit. It's useful because it never admits a failure it can't prove, and remediates real harm. |
| **Connected to ≥3 external apps** | **Four, all live:** Sentry (errors), Datadog (incidents), Mixpanel (feature usage) as read tools; Stripe (subscription + credit) for account context and the remedy action |
| **Show how you know it works** | Five code checks on every verdict; offline tests; evals (0 false remediations); `nexus verify` re-reads every citation from the live apps; a step trace for every run |

## 1. Architecture

```
Customer clicks "Cancel subscription" ─► GET /api/runs/stream?scenario=<customer>   (Server-Sent Events)
                                             │
            code: load account from STRIPE (plan, price, renewal, region, prior Nexus credits)
                                             │
      ┌──────────────────── NEXUS AGENT (Claude Sonnet 5, ≤10 turns) ────────────────────┐
      │ loop: read evidence board → choose tools → results added to board with IDs       │
      │                                                                                  │
      │ SENTRY    sentry_find_user_issues · sentry_get_issue                             │
      │ DATADOG   datadog_find_incidents · datadog_get_incident                          │
      │ MIXPANEL  mixpanel_feature_usage                                                 │
      │                                                                                  │
      │ required before submit: mixpanel_feature_usage and try_to_disprove               │
      │ ends with submit_verdict(claim, harm_ids, usage_ids, confidence, reasoning)      │
      └──────────────────────────────────────┬───────────────────────────────────────────┘
                                             ▼
                      FIVE CHECKS (code): recomputed from evidence, stop at first failure
                           │ fail / error                         │ all pass
                           ▼                                      ▼
                  standard offer (config)             REMEDIATION (code): what broke, proof it's fixed, credit
                  e.g. 20% off × 3 months                         │
                                                     customer clicks "Keep Pro with credit"
                                                                  ▼
                                               STRIPE (code): $60 balance credit, idempotent
```

**Why the agent never writes:** the agent only has read tools. Stripe writes run in code, only after the checks
pass *and* the customer clicks. Nothing can write while it's still investigating.

## 2. The agent

### 2.1 Tools (read-only, each returns evidence items with stable IDs)

| Tool | App | Call | Returns |
| --- | --- | --- | --- |
| `sentry_find_user_issues(user_id, since, until)` | Sentry | Discover events API, filtered by `user.id` | issues on this customer's account: type, title, count |
| `sentry_get_issue(issue_id)` | Sentry | Discover events + releases | count per day, peak day, fix release, events since the fix |
| `datadog_find_incidents(region, since, until)` | Datadog | `GET /api/v2/incidents` | declared incidents: title, severity, region |
| `datadog_get_incident(incident_id)` | Datadog | `GET /api/v2/incidents/{id}` | start and end of the outage (parsed from the title on the trial plan) |
| `mixpanel_feature_usage(user_id, since, until, day)` | Mixpanel | Export API, event `feature_used` | each feature's share of the team's activity; teammates active on `day` |
| `try_to_disprove(claim_summary)` | none (code) | Returns the questions the agent must answer: errors on their account, region and timing, how much the feature matters, whether anyone was working, other reasons | forces a self-check step |
| `submit_verdict(...)` | none (terminal) | pydantic-validated | `Verdict` |

### 2.2 Loop rules

- Budget of 10 turns; a "last chance: submit with what you have" message is added before the final turn.
- A `submit_verdict` in the same turn as other tool calls is refused ("you haven't seen those results").
- `submit_verdict` is refused until `mixpanel_feature_usage` and `try_to_disprove` have both run.
- An invalid verdict comes back to the agent as a validation error so it can fix it.
- Tool errors (timeout, 4xx/5xx, rate limit) come back as error results, never crashes. The agent can continue.
- Claude API error or budget exhausted: `insufficient_evidence`, which means the standard offer.

### 2.3 System prompt outline (`backend/src/nexus/agent/prompts/agent.md`)

1. Role: decide whether *our* technical failures caused this cancellation. Most cancellations aren't our fault; `no_harm` is the expected answer.
2. What each app tells you: Sentry (did this customer hit the failure), Datadog (confirmed outage in their region), Mixpanel (did it break something they depend on).
3. Procedure: errors on their account → outages in their region and window → feature usage on that day → **try to disprove** → submit.
4. Grounding: cite only evidence IDs that appear on the board. A confidence scale describes what was observed, not how good the story sounds.
5. Claims: `causal_harm` / `no_harm` / `insufficient_evidence`, with when to use each.

### 2.4 Code around the agent

| Step | What | Where |
| --- | --- | --- |
| Account context | Stripe: customer, subscription, price, renewal date, prior credits with incident metadata | `apps/live.py` |
| Five checks | Recomputed from board evidence; stop at the first failure | `decide/gates.py` |
| Remediation | Headline, letter, proof lines and fixed line built in code from cited evidence numbers only; credit amount from config; **no second credit** for the same incident | `decide/remediation.py` |
| Standard offer | From `business.toml`; offer copy that admits fault is rejected when the config loads | `decide/remediation.py`, `config.py` |
| Actions | "Keep Pro with credit" → Stripe balance transaction with an idempotency key. Discount → coupon. "Cancel" → `cancel_at_period_end=true` | `apps/stripe_actions.py`, `POST /api/actions` |

**The five checks:**
1. Every claim points to real data (each cited ID was returned by a tool in this run)
2. This customer hit the failure (Sentry: ≥10 errors on their account in the window)
3. It was a confirmed outage in their region (Datadog: region match, within a day of their errors)
4. It broke something they depend on (Mixpanel: the broken feature is ≥50% of their activity, ≥1 teammate active that day)
5. The evidence is strong enough (confidence ≥0.75)

## 3. Contracts

```python
class Account(BaseModel):   user_id: str; name: str; region: str; seats: int; plan: str; price_usd: int; renews: date; stripe_customer_id: str | None; prior_credit_incidents: list[str]
class Evidence(BaseModel):  id: str; app: App; kind: str; summary: str; data: dict
class Step(BaseModel):      n: int; tool: str; app: App | None; args: dict; ok: bool; evidence_ids: list[str]; summary: str; ms: int; view: dict
class Verdict(BaseModel):   claim: Claim; harm_ids: list[str]; usage_ids: list[str]; confidence: float; reasoning: str
class GateResult(BaseModel):n: int; name: str; status: Literal["pass","fail","skipped"]; reason: str
class Decision(BaseModel):  remediate: bool; claim: Claim; gates: list[GateResult]; stopped_at: int | None
class Remediation(BaseModel): headline: str; letter: str; proof: list[ProofLine]; fixed_line: str | None; credit_line: str; credit_usd: int; accept_label: str; decline_label: str
class StandardOffer(BaseModel): kind: Literal["percent_off","pause","none"]; headline: str; body: str; accept_label: str; decline_label: str
```

**SSE events** (one stream feeds the demo page, CLI, replays and `examples/`):

| Event | Payload | UI moment in `demo/v2` |
| --- | --- | --- |
| `run.started` | account | Cancel button → "One moment…" |
| `step` | `Step` | tile for that app fills or updates; step trace row |
| `verdict` | `Verdict` | agent's proposed claim and confidence |
| `gate` ×5 | `GateResult` | check rows tick ✓ / ✗ / not needed, each with a dropdown of what happened |
| `decision` | `Decision` | verdict banner |
| `remediation` | `Remediation` | remediation sheet |
| `run.done` | `outcome, offer, blocked_naive_message` | standard offer sheet + ghost card, or nothing extra |
| `run.error` | message | standard offer |

**Demo page:** a step trace under the tiles shows each tool call as it happens. This is how judges *see* the
multi-step agent. Clicking a Sentry, Datadog or Mixpanel tile opens that app's evidence page (`/apps/{app}`).

## 4. The four live apps

### 4.1 Setup and seeding (`nexus seed`)

Demo data is seeded **relative to today**, because the real apps won't accept old dates. `nexus seed` is a dry run
by default; `--send` writes to the apps, and `--app` limits it to one.

| App | Account | Seeds |
| --- | --- | --- |
| Sentry | free developer org, project `mergeline-sync` | error events tagged with each customer's `user.id`. Fathom: 31 `SyncTimeoutError` on the outage day, fixed in release `v4.12`. Tally: 42 export warnings. Canvasly: 2 avatar upload errors. Quanta: none |
| Datadog | 14-day trial, Incident Management | `Sync jobs timing out`, us-east-1, 09:10–14:40 UTC on the outage day, resolved (window stored in the title) |
| Mixpanel | free project | 8 weeks of `feature_used` events per customer. Fathom: sync is 82% of activity, 4 teammates active on the outage day. Quanta: reports 45%, sync 30%. Tally: export 3%. Canvasly: profile 1% |
| Stripe | test mode | customers with `metadata.user_id`, Pro $30/mo subscriptions, coupon `nexus_standard_offer` |

`nexus stripe-reset` reverses demo credits, discounts and scheduled cancels before each rehearsal.

### 4.2 Test and replay data

- **Scenario fixtures** (`fixtures/scenarios/*.json`): the same four customers as plain JSON, served through the same
  interface as the live apps. They drive tests, evals, CI and `nexus run --fake`, with no keys.
- **Replays** (`replays/`, local only): every live run is saved as its event stream, and the demo page
  replays it with the same pacing, for free and offline.

Live mode (`?fresh` or `nexus run --live`) calls Claude and the real apps.

## 5. How we know it works (reliability)

| Proof | What it shows | Command / place |
| --- | --- | --- |
| Check unit tests | Each of the five checks, including invented citations and wrong regions | `pytest tests/test_gates.py` |
| Agent loop tests with a scripted model + fixture apps | Budget, submit guards, error handling, standard offer on failure | `pytest tests/test_loop.py tests/test_pipeline.py` |
| API and Stripe tests | Streaming, replay, credit requires a proven run, configured amount, reversal | `pytest tests/test_api.py tests/test_stripe_actions.py` |
| **Evals** on scenario fixtures | Target: **0 false remediations**, every scenario correct with Claude Sonnet 5 | `nexus eval` → `evals/results/latest.md` |
| **Real runs on live data** | Fathom → remediation; Quanta → standard offer | `examples/` |
| **Verify** | Every cited ID re-read from Sentry, Datadog and Mixpanel matches | `nexus verify <recording>` |
| Step trace | Multi-step reasoning, visible | demo page + `examples/*/events.jsonl` |
| CI | Lint, tests and offline Fathom/Quanta runs with no keys | `.github/workflows/ci.yml` |

**Scenarios:** `fathom` (remediation), `quanta` (no errors, no outage), `tally` (errors in a feature they
barely use, no outage in their region), `canvasly` (two minor errors). Only Fathom should get a remediation.

## 6. Repo layout

```
nexus/
├── README.md  AGENTS.md  PLAN.md  .env.example  .gitignore
├── .github/workflows/ci.yml
├── docs/  system-brief.md  stack.md
├── demo/v2/  index.html (demo page)  app.html (per-app evidence page)
├── fixtures/scenarios/*.json
├── examples/                         # captured real runs
├── evals/results/  latest.md  run_*.json
└── backend/
    ├── pyproject.toml  uv.lock
    ├── config/business.toml          # remediation, standard offer, feature map
    ├── src/nexus/
    │   ├── models.py  config.py  dates.py  cli.py  pipeline.py  evals.py  verify.py
    │   ├── agent/    loop.py  tools.py  board.py  claude.py  fake.py  prompts/agent.md
    │   ├── apps/     base.py  fixtures.py  live.py  mixpanel.py  stripe_actions.py
    │   ├── decide/   gates.py  remediation.py
    │   ├── seed/     plan.py  send.py
    │   └── api/      main.py
    └── tests/
```

## 7. Phases

### S: Spikes before any build (go/no-go each)
| # | Question | Fallback |
| --- | --- | --- |
| S1 | Can Sentry return per-user error counts per day, plus the fix release? | Discover events API filtered by `user.id` |
| S2 | Does the Datadog trial expose Incidents v2 with backdated start/end times? | Store the outage window in the incident title |
| S3 | Can Mixpanel import 8 weeks of backdated events and query them per user? | Export API, compute feature shares in code |
| S4 | Stripe test mode: balance credit with idempotency key, metadata lookup | none needed (low risk) |
| S5 | Agent loop latency and cost on Fathom | Fewer turns, smaller model |

### P0: Foundation
Python project with uv, config, contracts, `AGENTS.md`, `.env.example`, CI.
**Done when:** CI is green.

### P1: Decision core on scenario fixtures, scripted model
Evidence board, tool layer on fixtures, agent loop with a scripted model, five checks, remediation and standard offer, CLI `run`.
**Done when:** both demo runs produce the expected events with no keys; pytest green.

### P2: Demo page
Plain HTML demo page subscribed to the event stream; replay of recorded runs with pacing; per-app evidence pages.
**Done when:** a replayed run looks the same as a live one.

### P3: Live apps
`nexus seed`, the four live app clients, Stripe actions, `nexus stripe-reset`.
**Done when:** Fathom and Quanta run end to end against all four live apps.

### P4: Real agent + evals
Claude model, agent prompt with confidence scale, `nexus eval` on all four scenarios.
**Done when:** 0 false remediations and every scenario correct.

### P5: Verify, document and demo
Wire the page to live runs; record real runs into `examples/`; `nexus verify`; README, system brief and stack rationale.

## 8. Repo standards

- deterministic pipeline with an LLM inside
- decisions in code: the model proposes, the five checks decide
- grounding enforced in code: customer text uses only numbers from cited evidence
- one event stream for the page, CLI, replays and examples
- replay without keys
- verify through a second channel (`nexus verify`)
- `examples/` of captured real runs
- tests fake the outside world
- pydantic-settings config mirrored in `.env.example`
- `AGENTS.md`
- a lean frontend with no runtime dependencies
- idempotent Stripe actions with idempotency keys
- per-app timeouts; tool errors returned to the agent as data
- secrets only in `.env`; CI uses fixtures and no keys
- credit amounts only from config
- model and prompt version recorded in every eval result

**Definition of done for any change:**
- ruff + pytest green
- new behavior tested with fakes
- README updated if a claim changed
- `AGENTS.md` updated if commands changed

## 9. Out of scope

- Real billing webhook from a production product
- Auth / multi-tenant
- Non-technical churn reasons
- Proactive outreach and incident-cost ledger
- Writing back to Sentry/Datadog
- Not built yet: personal-data redaction before the model, audit log, `/health`, Docker, a prompt-injection eval
