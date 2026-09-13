"""Network side of seeding. Every step checks for existing seed data first, so re-running is safe."""

import json
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Any
from urllib.parse import urlparse

import httpx

from nexus.apps.base import AppError
from nexus.apps.http import get_json_or_none, request_json
from nexus.apps.live import datadog_client, list_datadog_incidents, sentry_client
from nexus.apps.mixpanel import MixpanelClient
from nexus.apps.stripe_actions import COUPON, find_customer, stripe_client
from nexus.config import Settings
from nexus.seed.plan import SEED_TAG, SeedPlan, datadog_incident, sentry_events, sentry_fix_releases, stripe_customer

PRODUCT_ID = "mergeline_pro"
PRICE_LOOKUP = "mergeline_pro_monthly"


@dataclass
class SeedProgress:
    app: str
    message: str = ""
    ok: bool | None = None


def _step(app: str, action: Callable[[], list[str]]) -> Iterator[SeedProgress]:
    try:
        for line in action():
            yield SeedProgress(app, line, ok=True)
    except Exception as exc:
        yield SeedProgress(app, str(exc), ok=False)


def send_plan(plan: SeedPlan, cfg: Settings, chosen: set[str]) -> Iterator[SeedProgress]:
    """Report each app's failure and continue; client setup failures still propagate to the caller."""
    if "stripe" in chosen:
        yield SeedProgress("stripe")
        stripe = StripeSeeder(cfg)
        yield from _step("stripe", lambda: stripe.ensure_catalog() + [stripe.ensure_customer(f) for f in plan.fixtures])
    if "mixpanel" in chosen:
        yield SeedProgress("mixpanel")
        mixpanel = MixpanelClient(cfg.mixpanel_project_token, cfg.mixpanel_project_id, region=cfg.mixpanel_region)
        yield from _step("mixpanel", lambda: [
            f"imported {mixpanel.import_events(plan.mixpanel_events)['num_records_imported']} events",
        ])
    if "datadog" in chosen:
        yield SeedProgress("datadog")
        yield from _step("datadog", lambda: [DatadogSeeder(cfg).ensure_incident(i) for i in plan.incidents])
    if "sentry" in chosen:
        yield SeedProgress("sentry")
        sentry = SentrySeeder(cfg)

        def sentry_steps() -> list[str]:
            if sentry.existing_seed_issues():
                return ["seed events already present, skipped sending"] + sentry.resolve_fixed(plan.fixtures)
            return [f"sent {sentry.send_events(plan.fixtures)} events"] + sentry.resolve_fixed(plan.fixtures)

        yield from _step("sentry", sentry_steps)


class SentrySeeder:
    def __init__(self, cfg: Settings) -> None:
        dsn = urlparse(cfg.sentry_dsn)
        self.public_key, self.ingest_host, self.project_id = dsn.username, dsn.hostname, dsn.path.strip("/")
        self.org, self.project = cfg.sentry_org, cfg.sentry_project
        self.api = sentry_client(cfg, timeout=30)
        self.ingest = partial(httpx.Client, timeout=30)

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        return request_json(self.api, "sentry", method, path, **kwargs)

    def existing_seed_issues(self) -> list[dict[str, Any]]:
        return self._call("GET", f"/projects/{self.org}/{self.project}/issues/",
                          params={"query": f"nexus_seed:{SEED_TAG}", "statsPeriod": ""})

    def send_events(self, fixtures: list[dict[str, Any]]) -> int:
        url = f"https://{self.ingest_host}/api/{self.project_id}/envelope/"
        auth = f"Sentry sentry_version=7, sentry_key={self.public_key}, sentry_client=nexus-seed/0.1"
        headers = {"X-Sentry-Auth": auth, "Content-Type": "application/x-sentry-envelope"}
        sent = 0
        for fixture in fixtures:
            for event in sentry_events(fixture):
                header = {"event_id": event["event_id"], "sent_at": datetime.now(UTC).isoformat()}
                body = "\n".join([json.dumps(header), json.dumps({"type": "event"}), json.dumps(event)]) + "\n"
                request_json(self.ingest, "sentry ingest", "POST", url, content=body, headers=headers)
                sent += 1
        return sent

    def resolve_fixed(self, fixtures: list[dict[str, Any]], wait_s: int = 90) -> list[str]:
        releases = f"/organizations/{self.org}/releases/"
        done = []
        for fixture in fixtures:
            for rel in sentry_fix_releases(fixture):
                if get_json_or_none(self.api, "sentry", f"{releases}{rel['version']}/") is None:
                    self._call("POST", releases, json={
                        "version": rel["version"], "projects": [self.project], "dateReleased": rel["date_released"],
                    })
                issue = self._wait_for_issue(rel["issue_type"], wait_s)
                self._call("PUT", f"/projects/{self.org}/{self.project}/issues/", params={"id": issue["id"]},
                           json={"status": "resolved", "statusDetails": {"inRelease": rel["version"]}})
                done.append(f"{issue['shortId']} resolved in {rel['version']}")
        return done

    def _wait_for_issue(self, issue_type: str, wait_s: int) -> dict[str, Any]:
        deadline = time.monotonic() + wait_s
        while True:
            issues = [i for i in self.existing_seed_issues() if i.get("metadata", {}).get("type") == issue_type]
            if issues:
                return issues[0]
            if time.monotonic() > deadline:
                raise AppError(f"Sentry never showed a {issue_type} issue; events may still be processing")
            time.sleep(5)


class DatadogSeeder:
    def __init__(self, cfg: Settings) -> None:
        self.api = datadog_client(cfg, timeout=30)

    def ensure_incident(self, incident: dict[str, Any]) -> str:
        payload = datadog_incident(incident)
        title = payload["data"]["attributes"]["title"]
        match = next((i for i in list_datadog_incidents(self.api) if i["attributes"].get("title") == title), None)
        if match:
            return f"exists: {title} ({match['id']})"
        created = request_json(self.api, "datadog", "POST", "/api/v2/incidents", json=payload)
        return f"created: {title} ({created['data']['id']})"


class StripeSeeder:
    def __init__(self, cfg: Settings) -> None:
        self.api = stripe_client(cfg)
        self._price_id: str | None = None

    def _get(self, path: str, **params: Any) -> Any:
        return request_json(self.api, "stripe", "GET", path, params=params)

    def _create(self, path: str, idempotency_key: str, data: dict[str, Any]) -> dict[str, Any]:
        return request_json(self.api, "stripe", "POST", path, data=data, headers={"Idempotency-Key": idempotency_key})

    def _price(self) -> str:
        if self._price_id is None:
            prices = self._get("/prices", **{"lookup_keys[]": PRICE_LOOKUP})["data"]
            self._price_id = prices[0]["id"] if prices else self._create("/prices", f"nexus-seed-{PRICE_LOOKUP}", {
                "product": PRODUCT_ID, "unit_amount": 3000, "currency": "usd", "recurring[interval]": "month",
                "lookup_key": PRICE_LOOKUP, "nickname": "Pro monthly",
            })["id"]
        return self._price_id

    def ensure_catalog(self) -> list[str]:
        out = []
        if get_json_or_none(self.api, "stripe", f"/products/{PRODUCT_ID}"):
            out.append("product exists")
        else:
            product = self._create("/products", f"nexus-seed-{PRODUCT_ID}",
                                   {"id": PRODUCT_ID, "name": "Mergeline Pro", "metadata[nexus_seed]": SEED_TAG})
            out.append(f"product {product['id']}")
        self._price()
        out.append("price $30/month ready")
        if get_json_or_none(self.api, "stripe", f"/coupons/{COUPON}"):
            out.append("coupon exists")
        else:
            coupon = self._create("/coupons", f"nexus-seed-{COUPON}", {
                "id": COUPON, "percent_off": 20, "duration": "repeating", "duration_in_months": 3,
                "name": "Stay for 20% off",
            })
            out.append(f"coupon {coupon['id']}")
        return out

    def ensure_customer(self, fixture: dict[str, Any]) -> str:
        user_id = fixture["account"]["user_id"]
        customer = find_customer(self.api, user_id) or self._create(
            "/customers", f"nexus-seed-customer-{user_id}", stripe_customer(fixture))
        subs = self._get("/subscriptions", customer=customer["id"], status="all")["data"]
        if not subs:
            self._create("/subscriptions", f"nexus-seed-subscription-{customer['id']}", {
                "customer": customer["id"], "items[0][price]": self._price(), "collection_method": "send_invoice",
                "days_until_due": 30, "metadata[nexus_seed]": SEED_TAG,
            })
        return f"{fixture['account']['name']}: {customer['id']} with Pro subscription"
