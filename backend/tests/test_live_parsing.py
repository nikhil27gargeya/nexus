
from nexus.apps.live import incident_view, sentry_issue_rows


def test_datadog_incident_region_comes_from_the_title():
    view = incident_view({"id": "abc", "attributes": {
        "public_id": 3, "title": "Sync jobs timing out (us-east-1) [2026-09-03T09:10:00Z → 2026-09-03T14:40:00Z]",
        "fields": {"severity": {"value": "SEV-2"}, "state": {"value": "resolved"}}}})
    assert view["id"] == "INC-3"
    assert (view["title"], view["region"]) == ("Sync jobs timing out", "us-east-1")
    assert view["start"] == "2026-09-03T09:10:00Z"


def test_incident_timestamps_convert_offsets_to_utc_and_drop_fractional_seconds():
    view = incident_view({"id": "abc", "attributes": {
        "customer_impact_start": "2026-09-03T00:10:00.125+02:00",
        "customer_impact_end": "2026-09-03T00:40:00.999+02:00",
        "resolved": "2026-09-03T01:00:00+02:00",
    }})
    assert view["start"] == "2026-09-02T22:10:00Z"
    assert view["end"] == "2026-09-02T22:40:00Z"
    assert view["resolved_at"] == "2026-09-02T23:00:00Z"


def test_sentry_discover_rows_become_issue_evidence():
    rows = [{"issue": "MERGELINE-SYNC-1", "title": "Sync job exceeded 120s timeout",
             "error.type": ["SyncTimeoutError"], "count()": 31,
             "min(timestamp)": "2026-09-03T09:10:00+00:00", "max(timestamp)": "2026-09-03T14:10:00+00:00"}]
    assert sentry_issue_rows(rows) == [{"id": "MERGELINE-SYNC-1", "type": "SyncTimeoutError",
                                        "title": "Sync job exceeded 120s timeout", "user_count": 31,
                                        "first_seen": "2026-09-03", "last_seen": "2026-09-03"}]


