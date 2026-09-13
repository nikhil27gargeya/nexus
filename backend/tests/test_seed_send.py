from datetime import date
from unittest.mock import Mock

import httpx
import pytest

from nexus.config import REPO_ROOT, Settings
from nexus.seed import send
from nexus.seed.plan import build_plan


@pytest.fixture
def plan():
    return build_plan(REPO_ROOT / "fixtures", date(2026, 9, 12))


@pytest.fixture
def clients(monkeypatch):
    clients = Mock()
    for app in ("stripe", "mixpanel", "datadog", "sentry"):
        name = "MixpanelClient" if app == "mixpanel" else f"{app.title()}Seeder"
        monkeypatch.setattr(send, name, Mock(return_value=getattr(clients, app)))
    clients.stripe.ensure_catalog.return_value = ["catalog ready"]
    clients.stripe.ensure_customer.side_effect = RuntimeError("customer failed")
    clients.mixpanel.import_events.return_value = {"num_records_imported": 12}
    clients.datadog.ensure_incident.return_value = "incident ready"
    clients.sentry.send_events.return_value = 47
    clients.sentry.resolve_fixed.return_value = ["release resolved"]
    return clients


@pytest.mark.parametrize("existing", [False, True])
def test_send_order_continues_after_app_failure_and_resolves_sentry(plan, clients, existing):
    clients.sentry.existing_seed_issues.return_value = [{"id": "seed"}] if existing else []
    progress = list(send.send_plan(plan, Settings(), {"sentry", "datadog", "mixpanel", "stripe"}))

    assert [p.app for p in progress if p.ok is None] == ["stripe", "mixpanel", "datadog", "sentry"]
    assert [p for p in progress if p.app == "stripe"] == [
        send.SeedProgress("stripe"), send.SeedProgress("stripe", "customer failed", ok=False),
    ]
    assert send.SeedProgress("mixpanel", "imported 12 events", ok=True) in progress
    clients.mixpanel.import_events.assert_called_once_with(plan.mixpanel_events)
    assert clients.datadog.ensure_incident.call_count == len(plan.incidents)
    clients.sentry.resolve_fixed.assert_called_once_with(plan.fixtures)
    if existing:
        clients.sentry.send_events.assert_not_called()
        assert send.SeedProgress("sentry", "seed events already present, skipped sending", ok=True) in progress
    else:
        clients.sentry.send_events.assert_called_once_with(plan.fixtures)
        assert send.SeedProgress("sentry", "sent 47 events", ok=True) in progress
    assert progress[-1] == send.SeedProgress("sentry", "release resolved", ok=True)


def test_only_the_selected_app_is_used(plan, clients):
    progress = list(send.send_plan(plan, Settings(), {"mixpanel"}))
    assert progress == [send.SeedProgress("mixpanel"), send.SeedProgress("mixpanel", "imported 12 events", ok=True)]
    assert clients.stripe.mock_calls == clients.datadog.mock_calls == clients.sentry.mock_calls == []


def test_client_setup_failure_still_propagates_after_the_heading(plan, clients, monkeypatch):
    monkeypatch.setattr(send, "StripeSeeder", Mock(side_effect=RuntimeError("setup failed")))
    progress = send.send_plan(plan, Settings(), {"stripe", "mixpanel"})
    assert next(progress) == send.SeedProgress("stripe")
    with pytest.raises(RuntimeError, match="setup failed"):
        next(progress)
    assert clients.mixpanel.mock_calls == []


def _fake_api(handler, sent):
    def record(request):
        sent.append(request)
        return handler(request)

    return lambda: httpx.Client(base_url="https://app.test", transport=httpx.MockTransport(record))


def test_datadog_finds_an_existing_incident_past_the_first_page(plan):
    incident = plan.incidents[0]
    title = send.datadog_incident(incident)["data"]["attributes"]["title"]

    def handler(request):
        if int(request.url.params["page[offset]"]) == 0:
            others = [{"id": str(i), "attributes": {"title": "other"}} for i in range(100)]
            return httpx.Response(200, json={"data": others})
        return httpx.Response(200, json={"data": [{"id": "match", "attributes": {"title": title}}]})

    sent = []
    seeder = send.DatadogSeeder(Settings())
    seeder.api = _fake_api(handler, sent)
    assert seeder.ensure_incident(incident) == f"exists: {title} (match)"
    assert [r.method for r in sent] == ["GET", "GET"]


def test_stripe_seed_fetches_first_and_creates_only_what_is_missing(plan):
    fixture = plan.fixtures[0]
    found = {"/products/mergeline_pro": {"id": "mergeline_pro"}, "/prices": {"data": [{"id": "price_1"}]},
             "/customers/search": {"data": []}, "/subscriptions": {"data": []}}
    created = {"/coupons": "nexus_standard_offer", "/customers": "customer_new", "/subscriptions": "sub_new"}

    def handler(request):
        path = request.url.path
        if request.method == "GET":
            return httpx.Response(200, json=found[path]) if path in found else httpx.Response(404, json={})
        return httpx.Response(200, json={"id": created[path]})

    sent = []
    seeder = send.StripeSeeder(Settings())
    seeder.api = _fake_api(handler, sent)
    assert seeder.ensure_catalog() == ["product exists", "price $30/month ready", "coupon nexus_standard_offer"]
    seeder.ensure_customer(fixture)
    posts = [(r.url.path, r.headers["Idempotency-Key"]) for r in sent if r.method == "POST"]
    assert posts == [("/coupons", "nexus-seed-nexus_standard_offer"),
                     ("/customers", f"nexus-seed-customer-{fixture['account']['user_id']}"),
                     ("/subscriptions", "nexus-seed-subscription-customer_new")]
    assert sum(1 for r in sent if r.url.path == "/prices") == 1
    assert b"price_1" in next(r for r in sent if r.method == "POST" and r.url.path == "/subscriptions").content


def test_sentry_creates_a_fix_release_only_when_it_is_missing(plan, monkeypatch):
    fixture = next(f for f in plan.fixtures if send.sentry_fix_releases(f))
    version = send.sentry_fix_releases(fixture)[0]["version"]
    sent = []
    seeder = send.SentrySeeder(Settings())
    seeder.api = _fake_api(lambda r: httpx.Response(404 if r.method == "GET" else 200, json={}), sent)
    monkeypatch.setattr(seeder, "_wait_for_issue", lambda issue_type, wait_s: {"id": "1", "shortId": "SYNC-1"})
    assert seeder.resolve_fixed([fixture]) == [f"SYNC-1 resolved in {version}"]
    assert [r.method for r in sent] == ["GET", "POST", "PUT"]
