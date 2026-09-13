"""Mixpanel for Mergeline, the fictional product Nexus protects.

Tracking plan (server-side, synthetic demo workspaces, distinct_id = workspace user_id, never an email):

- sign_up_completed: once, when the workspace is created. plan (str), seats (int), region (str)
- feature_used: someone on the team used a product feature. feature (str: sync, reports, dashboards, export,
  profile), member (str: teammate id), plan (str), region (str)

Nexus reads two things from it: each feature's share of the team's activity, and how many teammates were active
on a given day. Sentry and Datadog only see failures; Mixpanel is what tells us whether a failure mattered.
"""

import json
import uuid
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import partial
from typing import Any

import httpx

from nexus.apps.base import AppError
from nexus.apps.http import request
from nexus.models import Account

INSERT_NAMESPACE = uuid.UUID("5b6f1c1e-6a0e-4d7a-9a51-6e2b3f0c9d11")
WEEKS = 8
EXPORT_HOSTS = {"api": "data", "api-eu": "data-eu", "api-in": "data-in"}


def insert_id(event: str, distinct_id: str, ts_ms: int, extra: str = "") -> str:
    """Stable per event, so re-running the seed never creates duplicates (Mixpanel dedupes on $insert_id)."""
    return uuid.uuid5(INSERT_NAMESPACE, f"{event}|{distinct_id}|{ts_ms}|{extra}").hex


def event(name: str, distinct_id: str, at: datetime, **props: Any) -> dict[str, Any]:
    ts_ms = int(at.timestamp() * 1000)
    clean = {k: v for k, v in props.items() if v is not None and v != ""}
    key = insert_id(name, distinct_id, ts_ms, json.dumps(clean, sort_keys=True))
    return {"event": name, "properties": {"time": ts_ms, "distinct_id": distinct_id, "$insert_id": key, **clean}}


def _at(day: date, hour: int, minute: int) -> datetime:
    return datetime.combine(day, datetime.min.time(), UTC).replace(hour=hour, minute=minute)


def demo_feature_events(account: Account, usage: dict[str, Any], cancel: date, weeks: int = WEEKS) -> Iterator[dict]:
    """Synthetic activity matching a fixture's feature shares and active teammates per day, ending on `cancel`."""
    start = cancel - timedelta(days=7 * weeks)
    yield event("sign_up_completed", account.user_id, _at(start - timedelta(days=120), 10, 0),
                plan=account.plan, seats=account.seats, region=account.region)
    pinned = {date.fromisoformat(day): members for day, members in usage.get("active_members", {}).items()}
    features = sorted(usage["feature_share"].items(), key=lambda kv: -kv[1])
    per_week = usage.get("events_per_week", 60)

    def used(day: date, hour: int, minute: int, feature: str, member: int) -> dict[str, Any]:
        return event("feature_used", account.user_id, _at(day, hour, minute), feature=feature,
                     member=f"{account.user_id}-m{member}", plan=account.plan, region=account.region)

    for week in range(weeks):
        k = 0
        for feature, share in features:
            for _ in range(max(1, round(share * per_week))):
                day = start + timedelta(days=7 * week + (k * 3 + week) % 7)
                k += 1
                if day not in pinned and day < cancel:
                    yield used(day, 8 + k % 9, k % 60, feature, 1 + k % account.seats)
    for day, members in pinned.items():
        for member in range(1, members + 1):
            yield used(day, 9, member, features[0][0], member)


def feature_usage_from_events(rows: Iterable[dict[str, Any]], user_id: str, day: str) -> dict[str, Any]:
    """Share of activity per feature for one customer, plus distinct teammates active on `day`."""
    counts: Counter[str] = Counter()
    members: set[str] = set()
    for props in rows:
        if props.get("distinct_id") != user_id:
            continue
        counts[props.get("feature", "unknown")] += 1
        if datetime.fromtimestamp(props["time"], UTC).date().isoformat() == day:
            members.add(props.get("member", ""))
    total = sum(counts.values())
    shares = {feature: round(count / total, 2) for feature, count in counts.most_common()} if total else {}
    return {"share_by_feature": shares, "active_members_on_day": len(members), "day": day, "events": total}


@dataclass
class MixpanelClient:
    project_token: str
    project_id: str = ""
    service_account_user: str = ""
    service_account_secret: str = ""
    region: str = "api"
    timeout: float = 30.0

    def import_events(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        if not self.project_token:
            raise AppError("MIXPANEL_PROJECT_TOKEN is not set")
        imported, failed = 0, []
        params = {"strict": "1", **({"project_id": self.project_id} if self.project_id else {})}
        with httpx.Client(timeout=self.timeout) as client:
            for i in range(0, len(events), 2000):
                response = client.post(f"https://{self.region}.mixpanel.com/import", params=params,
                                       auth=(self.project_token, ""), json=events[i:i + 2000])
                try:
                    body = response.json()
                except ValueError as exc:
                    raise AppError(f"mixpanel import returned {response.status_code} without JSON") from exc
                if response.status_code >= 400 and "failed_records" not in body:
                    raise AppError(f"mixpanel import failed ({response.status_code}): {body}")
                imported += body.get("num_records_imported", 0)
                failed += body.get("failed_records", [])
        return {"num_records_imported": imported, "failed_records": failed}

    def feature_rows(self, since: str, until: str, user_id: str | None = None) -> list[dict[str, Any]]:
        """Raw feature_used event properties via the Export API (available on the free plan).

        With user_id, Mixpanel filters to that customer's events before sending them.
        """
        if not (self.service_account_user and self.service_account_secret and self.project_id):
            raise AppError("Reading Mixpanel needs MIXPANEL_PROJECT_ID and a service account")
        host = EXPORT_HOSTS.get(self.region)
        if host is None:
            raise AppError(f"Unknown MIXPANEL_REGION {self.region!r}; use one of {sorted(EXPORT_HOSTS)}")
        params = {"project_id": self.project_id, "from_date": since, "to_date": until,
                  "event": json.dumps(["feature_used"])}
        if user_id:
            params["where"] = f'properties["$distinct_id"] == {json.dumps(user_id)}'
        response = request(partial(httpx.Client, timeout=self.timeout), "mixpanel", "GET",
                           f"https://{host}.mixpanel.com/api/2.0/export", params=params,
                           auth=(self.service_account_user, self.service_account_secret))
        try:
            return [json.loads(line).get("properties", {}) for line in response.text.splitlines() if line.strip()]
        except ValueError as exc:
            raise AppError("mixpanel export returned a line that isn't JSON") from exc
