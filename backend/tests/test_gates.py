from datetime import date

from nexus.agent.board import Board
from nexus.config import DEFAULT_FEATURES, GateConfig
from nexus.decide.gates import evaluate
from nexus.models import Account, Evidence, Verdict

ACCOUNT = Account(user_id="u_1", name="Test Co", region="us-east-1", seats=6, renews=date(2026, 4, 13))
WINDOW, CANCEL = date(2026, 1, 16), date(2026, 3, 13)
USAGE_ID = "MXP-u_1-2026-03-03"


def _board(errors=31, error_type="SyncTimeoutError", region="us-east-1", sync_share=0.82, active=4,
           usage_day="2026-03-03") -> Board:
    board = Board()
    board.add(Evidence(id="SNTRY-1", app="sentry", kind="sentry_issue", summary="", data={
        "type": error_type, "user_count": errors, "first_seen": "2026-03-03", "peak_date": "2026-03-03"}))
    board.add(Evidence(id="INC-1", app="datadog", kind="incident", summary="", data={
        "title": "Sync jobs timing out", "region": region, "start": "2026-03-03T09:10:00Z",
        "end": "2026-03-03T14:40:00Z"}))
    board.add(Evidence(id=USAGE_ID, app="mixpanel", kind="feature_usage", summary="", data={
        "share_by_feature": {"sync": sync_share, "export": 0.03}, "active_members_on_day": active, "day": usage_day}))
    return board


def _decide(board=None, **verdict):
    base = {"claim": "causal_harm", "harm_ids": ["SNTRY-1", "INC-1"], "usage_ids": [USAGE_ID],
            "confidence": 0.9, "reasoning": "test"}
    return evaluate(Verdict(**(base | verdict)), board or _board(), ACCOUNT, WINDOW, CANCEL, GateConfig(),
                    DEFAULT_FEATURES)


def test_all_checks_pass_when_each_app_confirms_its_part():
    decision = _decide()
    assert decision.remediate
    assert [g.status for g in decision.gates] == ["pass"] * 5
    assert decision.gates[3].reason == "data syncing is 82% of their activity · 4 of 6 active"


def test_an_invented_citation_stops_everything():
    decision = _decide(harm_ids=["SNTRY-1", "INC-1", "SNTRY-MADE-UP"])
    assert decision.stopped_at == 1
    assert [g.status for g in decision.gates[1:]] == ["skipped"] * 4


def test_a_few_errors_are_not_a_hit():
    assert _decide(_board(errors=3)).stopped_at == 2


def test_outage_in_another_region_does_not_count():
    decision = _decide(_board(region="eu-west-1"))
    assert decision.stopped_at == 3
    assert "customer is in us-east-1" in decision.gates[2].reason


def test_failure_in_a_feature_they_rarely_use_is_not_a_remediation():
    decision = _decide(_board(error_type="ExportRenderWarning"))
    assert decision.stopped_at == 4
    assert decision.gates[3].reason == "export is only 3% of their activity"


def test_nobody_working_that_day_is_not_a_remediation():
    assert _decide(_board(active=0)).stopped_at == 4


def test_usage_must_be_checked_for_the_day_it_broke():
    assert _decide(_board(usage_day="2026-02-01")).stopped_at == 4


def test_code_never_remediates_when_the_agent_did_not_claim_harm():
    assert not _decide(claim="insufficient_evidence").remediate


def test_low_confidence_means_no_remediation():
    assert _decide(confidence=0.5).stopped_at == 5
