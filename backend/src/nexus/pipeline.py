from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

from nexus.agent.board import Board
from nexus.agent.claude import ClaudeModel
from nexus.agent.fake import FakeModel
from nexus.agent.loop import run_agent
from nexus.agent.tools import Toolbox
from nexus.apps.base import Apps
from nexus.apps.fixtures import FixtureApps
from nexus.config import Settings, load_business, settings
from nexus.decide.gates import evaluate
from nexus.decide.remediation import build_remediation, build_standard_offer
from nexus.models import Account, NexusEvent

PROMPT = (Path(__file__).parent / "agent" / "prompts" / "agent.md").read_text()
NAIVE_BOT = "We're sorry for any inconvenience you may have experienced… here's 20% off."
TASK = ("Customer {name} (user_id {user_id}, region {region}, {plan} plan) clicked cancel on {cancel}. "
        "Investigate {since} to {cancel}. Did our technical failures cause this cancellation?")


def _standard(account: Account | None, cfg: Settings) -> NexusEvent:
    business = load_business(cfg.business_config)
    offer = build_standard_offer(account, business.standard_offer) if account else None
    return NexusEvent(type="run.done", data={
        "outcome": "standard_offer",
        "offer": offer.model_dump() if offer else None,
        "blocked_naive_message": NAIVE_BOT,
    })


def run(scenario: str, *, fake: bool = False, live: bool = False, cfg: Settings = settings) -> Iterator[NexusEvent]:
    """One cancellation, end to end. Every failure path ends in the standard offer, never a claim of fault."""
    account: Account | None = None
    try:
        business = load_business(cfg.business_config)
        fixtures = FixtureApps.load(cfg.fixtures_dir, scenario)
        apps: Apps = fixtures
        if live:
            from nexus.apps.live import LiveApps

            apps = LiveApps(cfg, fixtures.data["account"]["user_id"])
        account = apps.stripe_account()
        cancel = apps.cancel_date()
        window_start = cancel - timedelta(days=cfg.window_days)
        board = Board()
        toolbox = Toolbox(apps, board)
        model = (FakeModel(board, account, window_start, cancel, cfg.gates, business.features) if fake
                 else ClaudeModel(cfg.anthropic_api_key, cfg.agent_model, cfg.agent_max_tokens))
    except Exception as exc:
        yield NexusEvent(type="run.error", data={"message": str(exc)})
        yield _standard(account, cfg)
        return

    yield NexusEvent(type="run.started", data={
        "scenario": scenario,
        "account": account.model_dump(mode="json"),
        "window": {"since": window_start.isoformat(), "until": cancel.isoformat()},
        "model": "fake" if fake else cfg.agent_model,
        "data": "live" if live else "fixtures",
    })

    try:
        task = TASK.format(name=account.name, user_id=account.user_id, region=account.region, plan=account.plan,
                           cancel=cancel.isoformat(), since=window_start.isoformat())
        verdict = yield from run_agent(model, toolbox, system=PROMPT, task=task, max_turns=cfg.agent_max_turns)
        decision = evaluate(verdict, board, account, window_start, cancel, cfg.gates, business.features)
    except Exception as exc:
        yield NexusEvent(type="run.error", data={"message": str(exc)})
        yield _standard(account, cfg)
        return

    for gate in decision.gates:
        yield NexusEvent(type="gate", data=gate.model_dump())
    yield NexusEvent(type="decision", data=decision.model_dump())

    if decision.remediate:
        try:
            remediation = build_remediation(verdict, board, account, business.remediation, business.features)
            yield NexusEvent(type="remediation", data=remediation.model_dump())
            yield NexusEvent(type="run.done", data={"outcome": "remediation", "offer": None,
                                                    "blocked_naive_message": None})
            return
        except Exception as exc:
            yield NexusEvent(type="run.error", data={"message": f"remediation failed: {exc}"})
    yield _standard(account, cfg)
