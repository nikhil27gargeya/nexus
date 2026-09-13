import re
from datetime import date

import pytest

from nexus.apps.fixtures import FixtureApps
from nexus.apps.mixpanel import demo_feature_events, feature_usage_from_events
from nexus.config import REPO_ROOT

CANCEL = date(2026, 3, 13)


def _fathom():
    fixture = FixtureApps.load(REPO_ROOT / "fixtures", "fathom")
    return fixture.stripe_account(), fixture.data["mixpanel"]


def _rows(events):
    return [{**e["properties"], "time": e["properties"]["time"] / 1000} for e in events if e["event"] == "feature_used"]


def test_seeded_activity_reproduces_the_fixture_shares_and_active_teammates():
    account, usage = _fathom()
    result = feature_usage_from_events(_rows(demo_feature_events(account, usage, CANCEL)), "u_4812", "2026-03-03")
    assert result["share_by_feature"]["sync"] == pytest.approx(0.82, abs=0.03)
    assert result["active_members_on_day"] == 4


def test_events_follow_the_tracking_plan_rules():
    account, usage = _fathom()
    events = list(demo_feature_events(account, usage, CANCEL))
    assert {e["event"] for e in events} == {"sign_up_completed", "feature_used"}
    ids = [e["properties"]["$insert_id"] for e in events]
    assert len(ids) == len(set(ids)) and all(len(i) <= 36 and re.fullmatch(r"[0-9a-f]+", i) for i in ids)
    for e in events:
        props = e["properties"]
        assert props["distinct_id"] == "u_4812"
        assert all(v is not None and v != "" for v in props.values())
        assert all(re.fullmatch(r"\$?[a-z_]+", k) for k in props)


def test_reseeding_is_idempotent():
    account, usage = _fathom()
    first = [e["properties"]["$insert_id"] for e in demo_feature_events(account, usage, CANCEL)]
    assert first == [e["properties"]["$insert_id"] for e in demo_feature_events(account, usage, CANCEL)]


def test_usage_ignores_other_customers():
    rows = [{"distinct_id": "u_1", "feature": "sync", "member": "a", "time": 1788739200},
            {"distinct_id": "u_2", "feature": "reports", "member": "b", "time": 1788739200}]
    result = feature_usage_from_events(rows, "u_1", "2026-09-07")
    assert result["share_by_feature"] == {"sync": 1.0}
    assert result["active_members_on_day"] == 1
