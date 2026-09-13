import json

import pytest

from nexus import pipeline
from nexus.config import REPO_ROOT, Settings, StandardOfferConfig, fault_words_in


def _events(scenario, cfg=None):
    return list(pipeline.run(scenario, fake=True, **({"cfg": cfg} if cfg else {})))


def _first(events, kind):
    return next(e for e in events if e.type == kind).data


@pytest.mark.parametrize(
    ("scenario", "remediate", "stopped_at"),
    [
        ("fathom", True, None),
        ("quanta", False, 2),
        ("tally", False, 3),
        ("canvasly", False, 2),
    ],
)
def test_scenarios_match_the_demo_prototype(scenario, remediate, stopped_at):
    events = _events(scenario)
    decision = _first(events, "decision")
    assert decision["remediate"] is remediate
    assert decision["stopped_at"] == stopped_at
    assert events[-1].type == "run.done"
    assert events[-1].data["outcome"] == ("remediation" if remediate else "standard_offer")


def test_the_agent_takes_multiple_steps_across_three_apps():
    steps = [e.data for e in _events("fathom") if e.type == "step"]
    assert {s["app"] for s in steps} >= {"sentry", "datadog", "mixpanel"}
    assert len(steps) >= 5


def test_remediation_only_uses_numbers_from_the_evidence():
    remediation = _first(_events("fathom"), "remediation")
    assert "31 of your syncs failed" in remediation["letter"]
    assert "Keeping your apps in sync" in remediation["letter"]
    assert remediation["fixed_line"] == "Fixed on Mar 5. No sync failures since."
    assert remediation["credit_usd"] == 60
    assert all("SNTRY" not in p["text"] and "INC-" not in p["text"] for p in remediation["proof"])


def test_no_proof_still_gets_the_business_offer_without_admitting_fault():
    offer = _events("quanta")[-1].data["offer"]
    assert offer["headline"] == "Before you go, stay for 20% off"
    assert fault_words_in(" ".join([offer["headline"], offer["body"], offer["accept_label"]])) == []


def test_a_standard_offer_that_admits_fault_is_rejected_at_config_time():
    with pytest.raises(ValueError, match="must not admit fault"):
        StandardOfferConfig(headline="Sorry about the outages, have 20% off")


def test_the_event_stream_never_carries_the_stripe_customer_id(monkeypatch):
    real_stripe_account = pipeline.FixtureApps.stripe_account

    def with_id(self):
        return real_stripe_account(self).model_copy(update={"stripe_customer_id": "cus_secretish"})

    monkeypatch.setattr(pipeline.FixtureApps, "stripe_account", with_id)
    events = _events("fathom")
    assert "stripe_customer_id" not in _first(events, "run.started")["account"]
    assert all("cus_secretish" not in json.dumps(e.data, default=str) for e in events)


def test_fault_words_match_whole_words_only():
    assert fault_words_in("Sorry for the Outages. That's on us.") == ["on us", "outages", "sorry"]
    assert fault_words_in("We focus on usability, and coincidentally it's error-free") == ["error"]
    assert fault_words_in("We focus on usability and a terrorific new dashboard") == []


def test_apps_down_means_standard_offer(tmp_path):
    data = json.loads((REPO_ROOT / "fixtures/scenarios/fathom.json").read_text())
    data["fail"] = ["sentry", "datadog"]
    (tmp_path / "scenarios").mkdir()
    (tmp_path / "scenarios/fathom.json").write_text(json.dumps(data))
    events = _events("fathom", Settings(fixtures_dir=tmp_path))
    assert _first(events, "decision")["remediate"] is False
    assert events[-1].data["outcome"] == "standard_offer"


def test_unknown_scenario_is_standard_offer_not_a_crash():
    events = _events("does_not_exist")
    assert [e.type for e in events] == ["run.error", "run.done"]
    assert events[-1].data["outcome"] == "standard_offer"
