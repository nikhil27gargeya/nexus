"""HTTP API: serves the demo page and streams one cancellation run as Server-Sent Events.

Every completed run is saved to replays/. By default a saved run is replayed with demo pacing instead of calling
Claude again, which keeps rehearsals free and makes the demo work offline. Pass fresh=true to run the agent.
With no local replay, the committed real run in examples/ is used, so a fresh clone replays with no keys.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse
from starlette.concurrency import iterate_in_threadpool

from nexus import pipeline
from nexus.apps.fixtures import iter_scenarios, load_scenario
from nexus.apps.stripe_actions import StripeActions
from nexus.config import REPO_ROOT, load_business, settings
from nexus.models import NexusEvent

REPLAYS = REPO_ROOT / "replays"
EXAMPLES = REPO_ROOT / "examples"
DEMO_PAGE = REPO_ROOT / "demo" / "v2" / "index.html"
PACE = {"step": 0.7, "gate": 0.3, "decision": 0.5, "remediation": 0.4, "run.done": 0.4}

app = FastAPI(title="Nexus")
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins.split(","), allow_methods=["GET", "POST"])


class ActionRequest(BaseModel):
    scenario: str
    outcome: Literal["remediation", "standard_offer"]
    choice: Literal["keep", "cancel"]
    incident: str | None = None


def _proven_incidents(scenario: str) -> set[str]:
    """Incidents a recorded run for this customer proved and cited. Credits are only ever applied for these."""
    proven: set[str] = set()
    example = _example(scenario)
    for path in [*REPLAYS.glob(f"{scenario}.*.jsonl"), *([example] if example else [])]:
        events = [NexusEvent.model_validate_json(line) for line in path.read_text().splitlines() if line.strip()]
        done = next((e.data for e in events if e.type == "run.done"), {})
        verdict = next((e.data for e in events if e.type == "verdict"), {})
        if done.get("outcome") == "remediation":
            proven |= set(verdict.get("harm_ids", []))
    return proven


@app.post("/api/actions")
def act(request: ActionRequest) -> list[dict[str, Any]]:
    path = settings.fixtures_dir / "scenarios" / f"{request.scenario}.json"
    if not path.exists():
        raise HTTPException(404, f"Unknown scenario {request.scenario!r}")
    fixture = load_scenario(settings.fixtures_dir, request.scenario)
    user_id = fixture["account"]["user_id"]
    if request.outcome == "remediation" and request.incident not in _proven_incidents(request.scenario):
        raise HTTPException(409, "No recorded run proved harm for that incident; no credit applied")
    credit_usd = load_business(settings.business_config).remediation.credit_months * fixture["account"]["price_usd"]
    actions = StripeActions(settings)
    try:
        results = []
        if request.outcome == "remediation":
            results.append(actions.credit(user_id, credit_usd, request.incident))
        elif request.choice == "keep":
            results.append(actions.standard_offer(user_id))
        if request.choice == "cancel":
            results.append(actions.schedule_cancel(user_id))
        return results
    except Exception as exc:
        return [{"kind": "error", "ok": False, "stripe_id": None, "detail": str(exc)}]


def _recording(scenario: str, data: str, agent: str) -> Path:
    return REPLAYS / f"{scenario}.{data}.{agent}.jsonl"


def _example(scenario: str) -> Path | None:
    """The committed real run (Claude on live data) for this customer, if there is one."""
    return next(iter(sorted(EXAMPLES.glob(f"*-{scenario}-*/events.jsonl"))), None)


def _saved_run(scenario: str, data: str, agent: str) -> Path | None:
    """A local replay first; for Claude on live data, fall back to the committed example."""
    local = _recording(scenario, data, agent)
    if local.exists():
        return local
    return _example(scenario) if (data, agent) == ("live", "claude") else None


def _record(scenario: str, data: str, agent: str, fake: bool, live: bool) -> Iterator[NexusEvent]:
    events: list[NexusEvent] = []
    for event in pipeline.run(scenario, fake=fake, live=live):
        events.append(event)
        yield event
    if not any(e.type in ("run.error", "agent.error") for e in events):
        REPLAYS.mkdir(exist_ok=True)
        _recording(scenario, data, agent).write_text("\n".join(e.model_dump_json() for e in events) + "\n")


async def _replay(path: Path) -> AsyncIterator[NexusEvent]:
    for line in path.read_text().splitlines():
        event = NexusEvent.model_validate_json(line)
        await asyncio.sleep(PACE.get(event.type, 0.2))
        yield event


@app.get("/")
def demo_page() -> FileResponse:
    return FileResponse(DEMO_PAGE)


@app.get("/apps/{app_name}")
def app_page(app_name: Literal["sentry", "datadog", "mixpanel"]) -> FileResponse:
    return FileResponse(DEMO_PAGE.with_name("app.html"))


@app.get("/api/runs/latest")
def latest_run(scenario: str) -> list[dict[str, Any]]:
    """The most recent recorded run for a customer, preferring real Claude runs on live data."""
    load_or_404(scenario)
    preferred = [_saved_run(scenario, data, agent) for data, agent in
                 (("live", "claude"), ("fixtures", "claude"), ("live", "fake"), ("fixtures", "fake"))]
    path = next((p for p in preferred if p), None)
    if path is None:
        raise HTTPException(404, f"No recorded run for {scenario!r}")
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def load_or_404(scenario: str) -> dict[str, Any]:
    if not (settings.fixtures_dir / "scenarios" / f"{scenario}.json").exists():
        raise HTTPException(404, f"Unknown scenario {scenario!r}")
    return load_scenario(settings.fixtures_dir, scenario)


@app.get("/api/scenarios")
def scenarios() -> list[dict[str, Any]]:
    out = []
    for scenario, data in iter_scenarios(settings.fixtures_dir):
        out.append({"key": scenario, "name": data["account"]["name"], "user_id": data["account"]["user_id"],
                    "region": data["account"]["region"], "expected": data.get("expected")})
    return out


@app.get("/api/business")
def business() -> dict[str, Any]:
    return load_business(settings.business_config).model_dump()


@app.get("/api/runs/stream")
async def stream(
    scenario: str,
    data: Literal["live", "fixtures"] = "live",
    agent: Literal["claude", "fake"] = "claude",
    fresh: bool = False,
) -> EventSourceResponse:
    if not (settings.fixtures_dir / "scenarios" / f"{scenario}.json").exists():
        raise HTTPException(404, f"Unknown scenario {scenario!r}")
    saved = _saved_run(scenario, data, agent)
    replayed = saved is not None and not fresh
    if replayed:
        source: AsyncIterator[NexusEvent] = _replay(saved)
    else:
        source = iterate_in_threadpool(_record(scenario, data, agent, fake=agent == "fake", live=data == "live"))

    async def frames() -> AsyncIterator[dict[str, str]]:
        yield {"event": "meta", "data": json.dumps({"replayed": replayed})}
        async for event in source:
            yield {"event": event.type, "data": json.dumps(event.data, default=str)}

    return EventSourceResponse(frames())
