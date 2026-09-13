import json

import pytest
from fastapi.testclient import TestClient

from nexus.api import main
from nexus.apps.stripe_actions import net_credited_incidents


def test_reversed_credits_no_longer_count_as_credited():
    txns = [{"metadata": {"nexus_incident": "INC-2"}}, {"metadata": {"nexus_reversal": "INC-2"}},
            {"metadata": {"nexus_incident": "INC-2"}}, {"metadata": {}}]
    assert net_credited_incidents(txns) == ["INC-2"]
    assert net_credited_incidents(txns[:2]) == []


class FakeStripe:
    calls: list = []

    def __init__(self, cfg):
        pass

    def credit(self, user_id, amount, incident):
        FakeStripe.calls.append(("credit", user_id, amount, incident))
        return {"kind": "credit", "ok": True, "stripe_id": "cbtxn_1", "detail": ""}

    def standard_offer(self, user_id):
        FakeStripe.calls.append(("offer", user_id))
        return {"kind": "discount", "ok": True, "stripe_id": "sub_1", "detail": ""}

    def schedule_cancel(self, user_id):
        FakeStripe.calls.append(("cancel", user_id))
        return {"kind": "cancel", "ok": True, "stripe_id": "sub_1", "detail": ""}


@pytest.fixture
def client(tmp_path, monkeypatch):
    FakeStripe.calls = []
    monkeypatch.setattr(main, "REPLAYS", tmp_path)
    monkeypatch.setattr(main, "EXAMPLES", tmp_path / "no-examples")
    monkeypatch.setattr(main, "StripeActions", FakeStripe)
    events = [{"type": "verdict", "data": {"harm_ids": ["INC-2", "MERGELINE-SYNC-1"]}},
              {"type": "run.done", "data": {"outcome": "remediation"}}]
    (tmp_path / "fathom.live.claude.jsonl").write_text("\n".join(json.dumps(e) for e in events))
    return TestClient(main.app)


def test_credit_is_applied_only_for_a_proven_incident_and_uses_the_configured_amount(client):
    ok = client.post("/api/actions", json={"scenario": "fathom", "outcome": "remediation", "choice": "keep",
                                           "incident": "INC-2"})
    assert ok.status_code == 200
    assert FakeStripe.calls == [("credit", "u_4812", 60, "INC-2")]
    refused = client.post("/api/actions", json={"scenario": "quanta", "outcome": "remediation", "choice": "keep",
                                                "incident": "INC-2"})
    assert refused.status_code == 409


def test_standard_offer_and_cancel_need_no_proof(client):
    client.post("/api/actions", json={"scenario": "quanta", "outcome": "standard_offer", "choice": "keep"})
    client.post("/api/actions", json={"scenario": "quanta", "outcome": "standard_offer", "choice": "cancel"})
    assert FakeStripe.calls == [("offer", "u_2291"), ("cancel", "u_2291")]


def test_unknown_scenario_does_not_call_stripe(client):
    response = client.post("/api/actions", json={
        "scenario": "nope", "outcome": "standard_offer", "choice": "keep",
    })
    assert response.status_code == 404
    assert response.json() == {"detail": "Unknown scenario 'nope'"}
    assert FakeStripe.calls == []
