# Why this stack

Nexus has one job: decide whether a cancelling customer was actually hurt by our failures, remediate when
they were, and not overcompensate (standard offer only) when they weren't. Every choice below is judged against that job and three constraints: the hackathon asks
for **one multi-step agent connected to at least three apps, with proof that it works**; the build is solo,
in one night; and admitting a failure that didn't happen is a bad signal as well as a failed measure on its own.

## At a glance

| Layer | Choice | Rejected alternatives |
| --- | --- | --- |
| Agent loop | Plain Python loop over the **Anthropic SDK** tool use | LangGraph, Claude Agent SDK, CrewAI / AutoGen |
| Agent model | **Claude Sonnet 5** | Opus 5, smaller models |
| Decision | **Pure-Python gates** after the agent | Letting the model decide alone |
| App access | **Direct REST clients** for Sentry, Datadog, Mixpanel, Stripe | Vendor MCP servers |
| API | **FastAPI + Server-Sent Events** | WebSockets, polling |
| Contracts | **Pydantic** models | Dataclasses, untyped dicts |
| Frontend | **The `demo/v2` page itself**: HTML, CSS, vanilla JS on the event stream | React / Next.js |
| CLI | **Typer + Rich** | argparse |
| Testing | **pytest** with scenario fixtures and a scripted fake model | Live-API tests only |
| Tooling | **uv**, **ruff**, GitHub Actions | pip/poetry, flake8/black |

## Agent loop: plain Python on the Anthropic SDK

The loop in `backend/src/nexus/agent/loop.py` is about 100 lines. Each turn, Claude receives the conversation
and the tool list, asks for tools, our code calls the real app, and the results go back. It ends when Claude
calls `submit_verdict` or the step budget runs out.
**Why not LangGraph.** LangGraph shines at branching, resumable workflows with checkpoints and human pauses.
Nexus's flow is fixed and short: load account → agent → gates → compose. A graph framework would add concepts
(state reducers, stream modes) without removing any code we'd otherwise write. The one thing we'd miss,
LangGraph Studio's visual trace, is covered by the step trace in the demo.

**Why not the Claude Agent SDK.** It is built for general-purpose agents with file, shell and MCP tools, and it
owns the loop. We want the loop *visible*, because the guards inside it are part of the product: submit refused
until the self-check ran, submit refused in the same turn as other tools, a step budget, tool errors returned
as results. Those are easier to write, test and explain in our own loop.

**Why not Pydantic AI.** It is the closest fit: typed tools, validated structured output and step limits. Most of
our guards could be written as its output validators and retries. We kept our own loop so each guard is a visible,
separately tested line of code rather than a hook inside a framework, and to avoid swapping a working loop the
night before the demo. It would be a reasonable choice for a longer-lived version.

**Why not CrewAI / AutoGen.** They coordinate several role-playing agents through conversation. The requirement
is one agent, and Nexus needs predictable, auditable decisions, not emergent dialogue.

## Model: Claude Sonnet 5

The agent reads structured tool output, chooses the next tool, and weighs timing and magnitude. Sonnet 5 handles
tool use and this kind of judgment well, at a latency and cost that fit a customer waiting on a cancel page.
Opus 5 would reason more deeply but slow every run. Because the gates below re-check the verdict in code, the
model doesn't need to be flawless: a weak verdict becomes the standard offer, never a false remediation. The model is one
setting (`AGENT_MODEL`), so switching is a config change.

## Decision: code gates after the agent

The agent proposes; code disposes. `decide/gates.py` recomputes five checks from the evidence the tools
actually returned: citations exist, the customer hit the failure (Sentry), it was a confirmed outage in their region (Datadog), it broke something they depend on (Mixpanel), confidence clears
the bar. It stops at the first failure. This is the difference between "an AI said so" and "an AI said so,
and here is the deterministic check it passed". It's also why the model choice is low-risk.

## App access: direct REST, not MCP

Sentry and Datadog both ship MCP servers, but they're designed for human-in-the-loop coding assistants.
Sentry's search tools run their own LLM inside Sentry's server, which would put a second, unaudited model
inside our evidence. Datadog's hosted MCP authenticates as a person via OAuth, which is awkward for an unattended
service. Direct REST calls give us exact queries, stable IDs for citations, timeouts and retries we control,
and responses we can record for tests. Stripe has no MCP need at all; its REST API has native idempotency keys,
which is how we guarantee a credit is applied exactly once.

## API: FastAPI + Server-Sent Events

The agent produces a stream of events (each step, each gate, the verdict). SSE is the simplest way to push a
one-way stream to a browser. It needs no client library (native `EventSource`) and works through proxies.
WebSockets would add two-way plumbing we don't need. FastAPI gives typed endpoints straight from the Pydantic
models, with an async server.

## Contracts: Pydantic

Every boundary is a Pydantic model: `Verdict`, `Step`, `GateResult`, `Decision`, `Remediation`, `NexusEvent`.
The agent's final answer is validated against `Verdict`'s JSON schema, so a malformed answer is rejected before
it reaches the gates. The same models serialize the event stream for the API, the CLI and recorded examples.

## Frontend: the demo page itself, no framework

The UX was designed and approved as a single HTML file (`demo/v2/index.html`). Porting it to React tonight would
spend hours reproducing what already works and looks right. Instead the page subscribes to the SSE stream with
native `EventSource` and renders the same elements from real events. No runtime dependencies at all, which suits
a one-night build.

## CLI: Typer + Rich

`nexus run fathom` prints the agent's steps, the gates and the verdict in a readable terminal trace.
That trace is a debugging tool, a fallback demo, and proof for the README. Typer turns typed functions into
commands; Rich renders the trace.

## Testing: fixtures + a scripted model

Tests must not depend on network, API keys or model randomness. Scenario data lives in `fixtures/`, and
`agent/fake.py` is a scripted model that makes the same tool calls a careful agent would. That lets pytest
check what matters deterministically: the agent visits all three apps, invented citations are refused, a
failing app means the standard offer, the remediation only uses evidence numbers, and offer copy that admits
fault is rejected. Real-model quality is measured separately
by evals against the same fixtures.

## Tooling: uv, ruff, GitHub Actions

`uv` resolves and installs in seconds and pins with a lockfile. `ruff` replaces flake8, isort and pyupgrade
with one fast tool. GitHub Actions runs lint and tests on every push with no keys, because tests use fixtures.