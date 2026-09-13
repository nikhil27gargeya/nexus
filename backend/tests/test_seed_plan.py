import json
from collections import Counter
from datetime import date

from nexus.apps.fixtures import load_scenario
from nexus.config import REPO_ROOT
from nexus.seed.plan import build_plan, datadog_incident, relive, sentry_events, sentry_fix_releases, stripe_customer

TODAY = date(2026, 9, 12)


def _fixture(name="fathom"):
    return load_scenario(REPO_ROOT / "fixtures", name)


def test_plan_keeps_scenario_order_and_last_matching_incident(tmp_path):
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    for name in ("b", "a"):
        fixture = _fixture()
        fixture["account"]["user_id"] = name
        fixture["datadog"]["incidents"][0]["id"] = name
        (scenarios / f"{name}.json").write_text(json.dumps(fixture))

    plan = build_plan(tmp_path, TODAY)
    assert [f["account"]["user_id"] for f in plan.fixtures] == ["a", "b"]
    assert len(plan.incidents) == 1
    assert plan.incidents[0]["id"] == "b"
    assert plan.incidents[0]["start"] == "2026-09-02T09:10:00Z"
    assert {e["properties"]["distinct_id"] for e in plan.mixpanel_events} == {"a", "b"}


def test_relive_moves_every_date_but_keeps_the_story():
    live = relive(_fixture(), TODAY)
    assert live["cancel_date"] == "2026-09-12"
    incident = live["datadog"]["incidents"][0]
    assert incident["start"] == "2026-09-02T09:10:00Z"
    assert list(live["sentry"]["issues"][0]["daily"]) == ["2026-09-02"]
    assert live["mixpanel"]["active_members"] == {"2026-09-02": 4}
    assert live["sentry"]["issues"][0]["id"] == "SNTRY-8841"


def test_sentry_events_match_recorded_counts_and_identify_the_user():
    live = relive(_fixture(), TODAY)
    events = sentry_events(live)
    counts = Counter(e["exception"]["values"][0]["type"] for e in events)
    assert counts == {"SyncTimeoutError": 31, "WebhookRetryExceeded": 16}
    assert all(e["user"]["id"] == "u_4812" and e["tags"]["nexus_seed"] == "v1" for e in events)
    assert len({e["event_id"] for e in events}) == len(events)
    sync_times = [e["timestamp"] for e in events if "Sync" in e["exception"]["values"][0]["type"]]
    assert max(sync_times) <= "2026-09-02T14:40:00Z"


def test_seeding_is_deterministic():
    live = relive(_fixture(), TODAY)
    assert [e["event_id"] for e in sentry_events(live)] == [e["event_id"] for e in sentry_events(live)]


def test_fix_release_is_dated_when_the_fix_shipped():
    assert sentry_fix_releases(relive(_fixture(), TODAY)) == [
        {"version": "v4.12", "issue_type": "SyncTimeoutError", "date_released": "2026-09-04T12:00:00Z"}]


def test_datadog_incident_payload_is_resolved_and_regional():
    attrs = datadog_incident(relive(_fixture(), TODAY)["datadog"]["incidents"][0])["data"]["attributes"]
    assert attrs["title"] == "Sync jobs timing out (us-east-1) [2026-09-02T09:10:00Z → 2026-09-02T14:40:00Z]"
    assert attrs["fields"]["state"]["value"] == "resolved"


def test_stripe_customer_uses_an_example_domain():
    customer = stripe_customer(_fixture("quanta"))
    assert customer["email"] == "billing@quanta.example"
    assert customer["metadata[user_id]"] == "u_2291"
