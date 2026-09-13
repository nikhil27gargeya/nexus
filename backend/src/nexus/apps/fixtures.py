import json
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

from nexus.apps.base import AppError as AppError
from nexus.models import Account


def load_scenario(fixtures_dir: Path, scenario: str) -> dict[str, Any]:
    path = fixtures_dir / "scenarios" / f"{scenario}.json"
    if not path.exists():
        raise FileNotFoundError(f"No scenario named {scenario!r} in {path.parent}")
    return json.loads(path.read_text())


def iter_scenarios(fixtures_dir: Path) -> Iterator[tuple[str, dict[str, Any]]]:
    for path in sorted((fixtures_dir / "scenarios").glob("*.json")):
        yield path.stem, load_scenario(fixtures_dir, path.stem)


def _in_window(day: str, since: str, until: str) -> bool:
    return since <= day[:10] <= until


class FixtureApps:
    """Recorded Sentry, Datadog, Mixpanel and Stripe data for one scenario. Same interface as the live clients."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.failing: set[str] = set(data.get("fail", []))

    @classmethod
    def load(cls, fixtures_dir: Path, scenario: str) -> "FixtureApps":
        return cls(load_scenario(fixtures_dir, scenario))

    def _check(self, app: str) -> None:
        if app in self.failing:
            raise AppError(f"{app} is unavailable (503)")

    def cancel_date(self) -> date:
        return date.fromisoformat(self.data["cancel_date"])

    def stripe_account(self) -> Account:
        self._check("stripe")
        return Account.model_validate(self.data["account"])

    def sentry_find_user_issues(self, user_id: str, since: str, until: str) -> list[dict[str, Any]]:
        self._check("sentry")
        if user_id != self.data["account"]["user_id"]:
            return []
        keys = ("id", "type", "title", "user_count", "first_seen", "last_seen")
        return [
            {k: i[k] for k in keys}
            for i in self.data["sentry"]["issues"]
            if _in_window(i["last_seen"], since, until)
        ]

    def sentry_get_issue(self, issue_id: str, user_id: str) -> dict[str, Any] | None:
        self._check("sentry")
        return next((i for i in self.data["sentry"]["issues"] if i["id"] == issue_id), None)

    def datadog_find_incidents(self, region: str | None, since: str, until: str) -> list[dict[str, Any]]:
        self._check("datadog")
        keys = ("id", "title", "region", "severity", "start", "end")
        return [
            {k: i[k] for k in keys}
            for i in self.data["datadog"]["incidents"]
            if (region is None or i["region"] == region) and _in_window(i["start"], since, until)
        ]

    def datadog_get_incident(self, incident_id: str) -> dict[str, Any] | None:
        self._check("datadog")
        return next((i for i in self.data["datadog"]["incidents"] if i["id"] == incident_id), None)

    def mixpanel_feature_usage(self, user_id: str, since: str, until: str, day: str) -> dict[str, Any]:
        self._check("mixpanel")
        m = self.data["mixpanel"]
        if user_id != self.data["account"]["user_id"]:
            return {"share_by_feature": {}, "active_members_on_day": 0, "day": day, "events": 0}
        return {"share_by_feature": m["feature_share"], "active_members_on_day": m["active_members"].get(day, 0),
                "day": day, "events": m.get("events_per_week", 60) * 8}
