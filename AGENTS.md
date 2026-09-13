# AGENTS.md

Guidance for coding agents (Claude Code, Codex, Cursor) working in this repository.

## Project

Nexus is a hackathon project. When a customer clicks "Cancel subscription", one Claude agent investigates Sentry
(errors), Datadog (incidents) and Mixpanel (feature usage) in a multi-step tool loop and submits a verdict. Code gates
re-check the verdict. Only a proven causal story produces a **remediation** (evidence, the fix, a Stripe credit);
everyone else gets the business's configurable standard retention offer, which never admits fault. Terminology:
always "remediation" (remediation-based observability) and "standard offer"; never words of contrition. `PLAN.md` is the original build plan, `docs/stack.md` explains the stack,
and `demo/v2/index.html` is the UX spec.

## Commands

From `backend/` (Python ≥3.12, uv):

```bash
uv sync                                   # install
uv run ruff check src tests                # lint: must pass before finishing a task
uv run pytest -q                          # tests: must pass before finishing a task
uv run nexus scenarios                    # list fixture scenarios
uv run nexus run fathom --fake             # offline: remediation, all five gates pass
uv run nexus run quanta --fake             # offline: standard offer, stopped at gate 2
uv run nexus run fathom --fake --json       # raw event stream
uv run nexus run quanta                    # Claude with fixtures; needs ANTHROPIC_API_KEY
uv run nexus seed                         # dry-run seed plan for all apps
uv run nexus seed --app sentry             # dry-run plan for one app
uv run nexus seed --app sentry --send      # write seed data to the selected live app
uv run nexus stripe-reset                  # undo demo clicks in Stripe test mode
uv run nexus eval [--fake] [--runs 3]      # grade every fixture scenario; writes evals/results/latest.md
uv run nexus verify ../examples/01-fathom-remediation/events.jsonl  # re-read cited evidence from live apps
uv run nexus serve                        # demo page and API at 127.0.0.1:8765
```

## Architecture

- `cli.py`: command options and terminal rendering. Seed preparation and execution live in `seed/`.
- `pipeline.py`: one cancellation end to end, as a generator of `NexusEvent`s. Load account → `run_agent` →
  `evaluate` (gates) → `build_remediation` or `build_standard_offer`. Caught pipeline failures yield `run.error`
  and fall back to `outcome="standard_offer"`; the fallback still requires valid business configuration.
- `agent/loop.py`: the tool loop. Guards: step budget, submit refused until `mixpanel_feature_usage` and
  `try_to_disprove` ran, submit refused in the same turn as other tools, invalid verdicts returned as tool
  errors. Budget exhausted → `insufficient_evidence`.
- `agent/tools.py`: tool schemas and handlers. Every handler adds evidence to the `Board` with a stable ID;
  the agent can only cite what is on the board. Tools are read-only.
- `agent/claude.py`: Anthropic SDK model. `agent/fake.py`: scripted model for tests and `--fake`.
- `apps/base.py`: the shared `Apps` protocol and `AppError`. `fixtures.py` owns scenario loading and sorted
  discovery and serves scenario data; `live.py` implements the same read interface. `http.py` gives every live
  call a closed connection, one paused retry and `AppError` on failure. `mixpanel.py` handles
  event import/export; `stripe_actions.py` handles customer clicks and demo resets.
- `seed/plan.py`: `build_plan` prepares a `SeedPlan` with shifted fixture dates, deduplicated incidents and
  Mixpanel events; the module also builds Sentry, Datadog and Stripe payloads. No network calls.
- `seed/send.py`: live seed clients and `send_plan`, which yields progress in Stripe → Mixpanel → Datadog →
  Sentry order. App operation failures are reported and seeding continues; client setup failures propagate.
- `api/main.py`: demo page, scenario/config endpoints, SSE streaming (saved runs replay from `replays/`, or from
  `examples/` on a fresh clone), and customer actions. Remediation
  credits require a recorded run proving the requested incident before `StripeActions` is called.
- `dates.py`: shared ISO parsing, UTC timestamp formatting and calendar-date extraction without timezone conversion.
- `decide/gates.py`: five gates, stop at the first failure, recomputed from board evidence: real citations, the
  customer hit the failure (Sentry), a confirmed outage in their region (Datadog), the broken feature is something
  they depend on (Mixpanel feature share and active teammates), confidence. Error types map to product features in
  `backend/config/business.toml`. `tests/test_gates.py` and `tests/test_pipeline.py` cover each path. The demo page's
  offline simulated mode still uses older illustrative logic; the live mode renders backend events.
- `decide/remediation.py`: the remediation, built only from cited evidence (no internal IDs in customer text),
  and the standard offer, built from `backend/config/business.toml`. `StandardOfferConfig` rejects offer copy
  that admits fault (sorry, outage, error…).

## Conventions

- The agent never gets write tools. Stripe writes happen in code, after gates pass and the customer clicks.
- Numbers and decisions live in code; the model gathers evidence and judges.
- Tests use fixtures and the fake model: no network, no keys.
- Git: check with user before committing or pushing code
