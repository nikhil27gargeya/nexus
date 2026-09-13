"""Live Sentry, Datadog, Mixpanel and Stripe behind the same interface as FixtureApps."""

import re
from datetime import UTC, date, datetime, timedelta
from functools import partial
from typing import Any
from urllib.parse import urlparse

import httpx

from nexus.apps.base import AppError
from nexus.apps.http import ClientFactory, collect_pages, request_json
from nexus.apps.mixpanel import MixpanelClient, feature_usage_from_events
from nexus.apps.stripe_actions import lookup_subscriber, net_credited_incidents, stripe_client
from nexus.config import Settings
from nexus.dates import utc_timestamp
from nexus.models import Account

_REGION = re.compile(r"\(([a-z]{2}-[a-z]+-\d)\)")
_WINDOW = re.compile(r"\[(\S+Z) → (\S+Z)\]")
DATADOG_PAGE_SIZE = 100


def _get(new_client: ClientFactory, app: str, path: str, **params: Any) -> Any:
    return request_json(new_client, app, "GET", path, params=params)


def sentry_client(cfg: Settings, timeout: float = 20) -> ClientFactory:
    return partial(httpx.Client, base_url="https://sentry.io/api/0", timeout=timeout,
                   headers={"Authorization": f"Bearer {cfg.sentry_auth_token}"})


def datadog_client(cfg: Settings, timeout: float = 20) -> ClientFactory:
    return partial(httpx.Client, base_url=f"https://api.{cfg.datadog_site}", timeout=timeout,
                   headers={"DD-API-KEY": cfg.datadog_api_key, "DD-APPLICATION-KEY": cfg.datadog_app_key})


def list_datadog_incidents(new_client: ClientFactory) -> list[dict[str, Any]]:
    """Every incident in the account, raw, fetched page by page."""
    def page(offset: int) -> list[dict[str, Any]]:
        return _get(new_client, "datadog", "/api/v2/incidents",
                    **{"page[size]": DATADOG_PAGE_SIZE, "page[offset]": offset}).get("data", [])

    return collect_pages(page, DATADOG_PAGE_SIZE)


def incident_view(raw: dict[str, Any]) -> dict[str, Any]:
    attrs = raw["attributes"]
    fields = attrs.get("fields") or {}
    title = attrs.get("title", "")
    region, window = _REGION.search(title), _WINDOW.search(title)
    start = attrs.get("customer_impact_start") or (window and window.group(1))
    end = attrs.get("customer_impact_end") or (window and window.group(2))
    return {
        "id": f"INC-{attrs.get('public_id', raw['id'])}",
        "title": _WINDOW.sub("", _REGION.sub("", title)).strip(),
        "region": region.group(1) if region else "unknown",
        "severity": (fields.get("severity") or {}).get("value") or "UNKNOWN",
        "start": utc_timestamp(start) if start else None,
        "end": utc_timestamp(end) if end else None,
        "resolved_at": utc_timestamp(attrs["resolved"]) if attrs.get("resolved") else None,
        "state": (fields.get("state") or {}).get("value"),
    }


def sentry_issue_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    issues = []
    for row in rows:
        error_type = row.get("error.type")
        issues.append({
            "id": row["issue"],
            "type": error_type[0] if isinstance(error_type, list) and error_type else (error_type or row["title"]),
            "title": row["title"],
            "user_count": int(row["count()"]),
            "first_seen": utc_timestamp(row["min(timestamp)"])[:10],
            "last_seen": utc_timestamp(row["max(timestamp)"])[:10],
        })
    return sorted(issues, key=lambda i: -i["user_count"])


class LiveApps:
    def __init__(self, cfg: Settings, user_id: str) -> None:
        self.cfg, self.user_id = cfg, user_id
        self.sentry = sentry_client(cfg)
        self.sentry_project_id = urlparse(cfg.sentry_dsn).path.strip("/")
        self.datadog = datadog_client(cfg)
        self.stripe = stripe_client(cfg)
        self.mixpanel = MixpanelClient(cfg.mixpanel_project_token, cfg.mixpanel_project_id,
                                       cfg.mixpanel_service_account_user, cfg.mixpanel_service_account_secret,
                                       cfg.mixpanel_region)

    def cancel_date(self) -> date:
        return datetime.now(UTC).date()

    def stripe_account(self) -> Account:
        found = lookup_subscriber(self.stripe, self.user_id)
        customer, sub = found.customer, found.subscription
        if not sub["items"]["data"]:
            raise AppError(f"stripe: {customer['name']} has a subscription with no price")
        item = sub["items"]["data"][0]
        product = _get(self.stripe, "stripe", f"/products/{item['price']['product']}")
        period_end = item.get("current_period_end") or sub.get("current_period_end")
        return Account(
            user_id=self.user_id,
            name=customer["name"],
            region=customer["metadata"].get("region", "unknown"),
            seats=int(customer["metadata"].get("seats", 1)),
            plan=product["name"].removeprefix("Mergeline ").strip() or "Pro",
            price_usd=item["price"]["unit_amount"] // 100,
            renews=datetime.fromtimestamp(period_end, UTC).date(),
            stripe_customer_id=customer["id"],
            prior_credit_incidents=net_credited_incidents(found.transactions),
        )

    def _sentry_events(self, fields: list[str], query: str, start: str, end: str) -> list[dict[str, Any]]:
        return _get(self.sentry, "sentry", f"/organizations/{self.cfg.sentry_org}/events/", field=fields,
                    query=query, project=self.sentry_project_id, start=start, end=end, dataset="errors",
                    per_page=100)["data"]

    def sentry_find_user_issues(self, user_id: str, since: str, until: str) -> list[dict[str, Any]]:
        end = (date.fromisoformat(until) + timedelta(days=1)).isoformat()
        rows = self._sentry_events(["issue", "title", "error.type", "count()", "min(timestamp)", "max(timestamp)"],
                                   f"user.id:{user_id}", f"{since}T00:00:00", f"{end}T00:00:00")
        return sentry_issue_rows(rows)

    def sentry_get_issue(self, issue_id: str, user_id: str) -> dict[str, Any] | None:
        org = self.cfg.sentry_org
        group = _get(self.sentry, "sentry", f"/organizations/{org}/shortids/{issue_id}/")["group"]
        since = (datetime.now(UTC) - timedelta(days=self.cfg.window_days + 7)).strftime("%Y-%m-%dT00:00:00")
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        rows = self._sentry_events(["timestamp.to_day", "count()"], f"issue:{issue_id} user.id:{user_id}", since, now)
        daily = {r["timestamp.to_day"][:10]: int(r["count()"]) for r in rows}
        if not daily:
            return None
        release = (group.get("statusDetails") or {}).get("inRelease")
        resolved_at, since_fix = None, None
        if group.get("status") == "resolved" and release:
            info = _get(self.sentry, "sentry", f"/organizations/{org}/releases/{release}/")
            resolved_at = utc_timestamp(info.get("dateReleased") or info["dateCreated"])[:10]
            after = self._sentry_events(["count()"], f"issue:{issue_id}", f"{resolved_at}T12:00:01", now)
            since_fix = int(after[0]["count()"]) if after else 0
        return {
            "id": issue_id,
            "type": group.get("metadata", {}).get("type") or group["title"],
            "title": group.get("metadata", {}).get("value") or group["title"],
            "user_count": sum(daily.values()),
            "first_seen": min(daily),
            "last_seen": max(daily),
            "peak_date": max(daily, key=daily.get),
            "daily": dict(sorted(daily.items())),
            "status": group.get("status"),
            "resolved_in_release": release if group.get("status") == "resolved" else None,
            "resolved_at": resolved_at,
            "events_since_fix": since_fix,
        }

    def _incidents(self) -> list[dict[str, Any]]:
        return [incident_view(i) for i in list_datadog_incidents(self.datadog)]

    def datadog_find_incidents(self, region: str | None, since: str, until: str) -> list[dict[str, Any]]:
        keys = ("id", "title", "region", "severity", "start", "end")
        return [{k: i[k] for k in keys} for i in self._incidents()
                if i["start"] and since <= i["start"][:10] <= until and (region is None or i["region"] == region)]

    def datadog_get_incident(self, incident_id: str) -> dict[str, Any] | None:
        return next((i for i in self._incidents() if i["id"] == incident_id), None)

    def mixpanel_feature_usage(self, user_id: str, since: str, until: str, day: str) -> dict[str, Any]:
        return feature_usage_from_events(self.mixpanel.feature_rows(since, until, user_id), user_id, day)
