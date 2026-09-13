from datetime import date
from typing import Any

from nexus.agent.board import Board
from nexus.agent.loop import ToolUse, Turn
from nexus.config import GateConfig
from nexus.decide.gates import evaluate, issue_day
from nexus.models import Account, Evidence, Verdict


class FakeModel:
    """A scripted stand-in for Claude: same tool calls a careful agent makes, no API key needed."""

    def __init__(self, board: Board, account: Account, window_start: date, cancel: date, cfg: GateConfig,
                 features: dict[str, str] | None = None) -> None:
        self.board, self.account, self.cfg, self.features = board, account, cfg, features or {}
        self.since, self.until = window_start.isoformat(), cancel.isoformat()
        self.window_start, self.cancel = window_start, cancel
        self._turn = 0

    def turn(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Turn:
        self._turn += 1
        calls = self._calls()
        uses = [ToolUse(id=f"fake_{self._turn}_{i}", name=name, input=args) for i, (name, args) in enumerate(calls)]
        raw = [{"type": "tool_use", "id": u.id, "name": u.name, "input": u.input} for u in uses]
        return Turn(text="", tool_uses=uses, raw=raw)

    def _hit(self) -> list[Evidence]:
        issues = [e for e in self.board.of_kind("sentry_issue") if e.data["user_count"] >= self.cfg.min_errors]
        return sorted(issues, key=lambda e: -e.data["user_count"])

    def _regional_incidents(self) -> list[Evidence]:
        return [e for e in self.board.of_kind("incident") if e.data["region"] == self.account.region]

    def _harm_day(self) -> str:
        if hit := self._hit():
            return issue_day(hit[0]).isoformat()
        incidents = self._regional_incidents()
        return incidents[0].data["start"][:10] if incidents else self.until

    def _calls(self) -> list[tuple[str, dict[str, Any]]]:
        user, window = self.account.user_id, {"since": self.since, "until": self.until}
        if self._turn == 1:
            return [
                ("sentry_find_user_issues", {"user_id": user, **window}),
                ("datadog_find_incidents", {"region": self.account.region, **window}),
            ]
        if self._turn == 2:
            calls = [("sentry_get_issue", {"issue_id": e.id, "user_id": user}) for e in self._hit()]
            calls += [("datadog_get_incident", {"incident_id": e.id}) for e in self._regional_incidents()]
            return calls + [("mixpanel_feature_usage", {"user_id": user, **window, "day": self._harm_day()})]
        if self._turn == 3:
            return [("try_to_disprove", {"claim_summary": "Checking whether the failure hit something they rely on."})]
        return [("submit_verdict", self._verdict())]

    def _verdict(self) -> dict[str, Any]:
        usage_id = f"MXP-{self.account.user_id}-{self._harm_day()}"
        candidate = Verdict(
            claim="causal_harm",
            harm_ids=[e.id for e in self._hit()] + [e.id for e in self._regional_incidents()],
            usage_ids=[usage_id] if usage_id in self.board else [],
            confidence=0.9,
            reasoning="Scripted verdict from the evidence board.",
        )
        decision = evaluate(candidate, self.board, self.account, self.window_start, self.cancel, self.cfg,
                            self.features)
        claim = "causal_harm" if decision.remediate else ("no_harm" if not self._hit() else "insufficient_evidence")
        return {**candidate.model_dump(), "claim": claim}
