from nexus.agent.board import Board
from nexus.agent.loop import ToolUse, Turn, run_agent
from nexus.agent.tools import Toolbox
from nexus.apps.fixtures import FixtureApps
from nexus.config import REPO_ROOT

VERDICT = {"claim": "no_harm", "harm_ids": [], "usage_ids": [], "confidence": 0.8, "reasoning": "nothing found"}
WEEKLY = ("mixpanel_feature_usage", {"user_id": "u_4812", "since": "2026-01-16", "until": "2026-03-13",
                                     "day": "2026-03-03"})
DISPROVE = ("try_to_disprove", {"claim_summary": "no harm"})


class Scripted:
    def __init__(self, *turns):
        self.turns = list(turns)

    def turn(self, system, messages, tools):
        calls = self.turns.pop(0) if self.turns else []
        uses = [ToolUse(id=f"t{i}", name=n, input=a) for i, (n, a) in enumerate(calls)]
        return Turn(text="", tool_uses=uses, raw=[{"type": "tool_use", "id": u.id, "name": u.name, "input": u.input}
                                                  for u in uses])


def _run(model, max_turns=10):
    toolbox = Toolbox(FixtureApps.load(REPO_ROOT / "fixtures", "fathom"), Board())
    gen = run_agent(model, toolbox, system="", task="go", max_turns=max_turns)
    events = []
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        return stop.value, events


def test_submit_is_refused_until_usage_and_self_check_are_done():
    verdict, events = _run(Scripted([("submit_verdict", VERDICT)], [WEEKLY], [DISPROVE], [("submit_verdict", VERDICT)]))
    assert verdict.claim == "no_harm"
    assert any(e.type == "rejected" and "try_to_disprove" in e.data["reason"] for e in events)


def test_submit_in_the_same_turn_as_other_tools_is_discarded():
    verdict, events = _run(Scripted([WEEKLY, DISPROVE, ("submit_verdict", VERDICT)], [("submit_verdict", VERDICT)]))
    assert any(e.type == "rejected" and "same turn" in e.data["reason"] for e in events)
    assert verdict.claim == "no_harm"


def test_running_out_of_steps_means_insufficient_evidence():
    verdict, _ = _run(Scripted(*[[WEEKLY]] * 3), max_turns=3)
    assert verdict.claim == "insufficient_evidence"


def test_a_failing_tool_is_reported_to_the_agent_not_raised():
    verdict, events = _run(Scripted([("sentry_get_issue", {"issue_id": "NOPE", "user_id": "u_4812"})]), max_turns=1)
    step = next(e for e in events if e.type == "step")
    assert step.data["ok"] is False
    assert verdict.claim == "insufficient_evidence"
