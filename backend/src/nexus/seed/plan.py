"""Pure builders for the demo data seeded into the live apps. No network here, so everything is testable.

Fixtures are written around a cancel date of 2026-03-13. For live apps, every date is shifted so the cancel
date becomes today; the relative story (outage 10 days before cancel) stays identical.
"""

import copy
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from nexus.apps.fixtures import iter_scenarios
from nexus.apps.mixpanel import demo_feature_events
from nexus.dates import parse_datetime, utc_timestamp
from nexus.models import Account

SEED_TAG = "v1"
NAMESPACE = uuid.UUID("0b1e7f4e-2c61-4a53-8d7c-4f9a3e1b6d20")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2})?Z?)?$")


@dataclass
class SeedPlan:
    cancel: date
    fixtures: list[dict[str, Any]]
    incidents: list[dict[str, Any]]
    mixpanel_events: list[dict[str, Any]]


def build_plan(fixtures_dir: Path, cancel: date) -> SeedPlan:
    fixtures = [relive(data, cancel) for _, data in iter_scenarios(fixtures_dir)]
    incidents = {
        incident["title"] + incident["region"]: incident
        for fixture in fixtures for incident in fixture["datadog"]["incidents"]
    }
    mixpanel_events = [
        event for fixture in fixtures
        for event in demo_feature_events(Account.model_validate(fixture["account"]), fixture["mixpanel"], cancel)
    ]
    return SeedPlan(cancel, fixtures, list(incidents.values()), mixpanel_events)


def _shift_str(value: str, days: int) -> str:
    if len(value) == 10:
        return (date.fromisoformat(value) + timedelta(days=days)).isoformat()
    moved = parse_datetime(value) + timedelta(days=days)
    return utc_timestamp(moved)


def _shift(obj: Any, days: int) -> Any:
    if isinstance(obj, dict):
        return {(_shift_str(k, days) if _DATE.match(k) else k): _shift(v, days) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_shift(v, days) for v in obj]
    if isinstance(obj, str) and _DATE.match(obj):
        return _shift_str(obj, days)
    return obj


def relive(fixture: dict[str, Any], cancel: date) -> dict[str, Any]:
    """The fixture with every date moved so its cancel date is `cancel`."""
    days = (cancel - date.fromisoformat(fixture["cancel_date"])).days
    return _shift(copy.deepcopy(fixture), days)


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def sentry_events(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    """One Sentry error event per recorded occurrence, spread across the day from 09:10 UTC every 10 minutes."""
    account = fixture["account"]
    events = []
    for issue in fixture["sentry"]["issues"]:
        for day, count in issue["daily"].items():
            start = datetime.fromisoformat(f"{day}T09:10:00+00:00")
            for i in range(count):
                at = start + timedelta(minutes=10 * i)
                events.append({
                    "event_id": uuid.uuid5(NAMESPACE, f"{account['user_id']}|{issue['type']}|{day}|{i}").hex,
                    "timestamp": utc_timestamp(at),
                    "level": "error",
                    "platform": "python",
                    "environment": "production",
                    "release": "v4.11",
                    "transaction": "sync.run",
                    "exception": {"values": [{"type": issue["type"], "value": issue["title"]}]},
                    "fingerprint": [issue["type"]],
                    "user": {"id": account["user_id"]},
                    "tags": {"region": account["region"], "workspace": account["name"], "nexus_seed": SEED_TAG},
                })
    return events


def sentry_fix_releases(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    """Releases that resolved an issue, with their real (shifted) release date."""
    return [
        {"version": i["resolved_in_release"], "issue_type": i["type"], "date_released": f"{i['resolved_at']}T12:00:00Z"}
        for i in fixture["sentry"]["issues"] if i.get("resolved_in_release")
    ]


def datadog_incident(incident: dict[str, Any]) -> dict[str, Any]:
    return {"data": {"type": "incidents", "attributes": {
        "title": f"{incident['title']} ({incident['region']}) [{incident['start']} → {incident['end']}]",
        "customer_impacted": True,
        "customer_impact_scope": f"Sync workers in {incident['region']}",
        "customer_impact_start": incident["start"],
        "customer_impact_end": incident["end"],
        "fields": {
            "severity": {"type": "dropdown", "value": incident["severity"]},
            "state": {"type": "dropdown", "value": "resolved"},
        },
    }}}


def stripe_customer(fixture: dict[str, Any]) -> dict[str, str]:
    a = fixture["account"]
    return {
        "name": a["name"],
        "email": f"billing@{slug(a['name'])}.example",
        "metadata[user_id]": a["user_id"],
        "metadata[region]": a["region"],
        "metadata[seats]": str(a["seats"]),
        "metadata[nexus_seed]": SEED_TAG,
    }
