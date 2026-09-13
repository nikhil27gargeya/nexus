import httpx
import pytest

from nexus.apps.base import AppError
from nexus.apps.http import collect_pages, request_json, retry_wait
from nexus.apps.mixpanel import MixpanelClient
from nexus.apps.stripe_actions import lookup_subscriber


def _factory(handler, opened):
    def new_client():
        client = httpx.Client(base_url="https://app.test", transport=httpx.MockTransport(handler))
        opened.append(client)
        return client
    return new_client


def test_rate_limit_waits_before_the_one_retry_and_closes_every_connection():
    replies = iter([httpx.Response(429, headers={"Retry-After": "2"}), httpx.Response(200, json={"ok": True})])
    opened, waits = [], []
    body = request_json(_factory(lambda request: next(replies), opened), "sentry", "GET", "/x", sleep=waits.append)
    assert body == {"ok": True}
    assert waits == [2.0]
    assert len(opened) == 2 and all(client.is_closed for client in opened)


def test_retry_wait_is_capped_and_defaults_to_one_second():
    assert retry_wait(httpx.Response(429, headers={"Retry-After": "600"})) == 5.0
    assert retry_wait(httpx.Response(503)) == 1.0
    assert retry_wait(httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})) == 1.0


def test_errors_become_app_errors_even_without_json():
    opened = []
    with pytest.raises(AppError, match="stripe 402: card declined"):
        request_json(_factory(lambda r: httpx.Response(402, json={"error": {"message": "card declined"}}), opened),
                     "stripe", "GET", "/x", sleep=lambda s: None)
    with pytest.raises(AppError, match="datadog 404: <html>not found"):
        request_json(_factory(lambda r: httpx.Response(404, text="<html>not found"), opened),
                     "datadog", "GET", "/x", sleep=lambda s: None)
    with pytest.raises(AppError, match="isn't JSON"):
        request_json(_factory(lambda r: httpx.Response(200, text="plain"), opened), "sentry", "GET", "/x")


def test_pages_are_fetched_until_one_comes_back_short():
    offsets = []

    def page(offset):
        offsets.append(offset)
        return list(range(100)) if offset < 200 else [1, 2]

    assert len(collect_pages(page, 100)) == 202 and offsets == [0, 100, 200]
    assert len(collect_pages(lambda offset: list(range(100)), 100, max_pages=3)) == 300


def test_one_stripe_lookup_returns_customer_subscription_and_credits():
    def handler(request):
        path = request.url.path
        if path == "/customers/search":
            assert "u_4812" in request.url.params["query"]
            return httpx.Response(200, json={"data": [{"id": "cus_1", "name": "Fathom"}]})
        if path == "/subscriptions":
            return httpx.Response(200, json={"data": [{"id": "sub_1"}]})
        return httpx.Response(200, json={"data": [{"metadata": {"nexus_incident": "INC-2"}}]})

    found = lookup_subscriber(_factory(handler, []), "u_4812")
    assert (found.customer["id"], found.subscription["id"], len(found.transactions)) == ("cus_1", "sub_1", 1)
    with pytest.raises(AppError, match="no customer"):
        lookup_subscriber(_factory(lambda r: httpx.Response(200, json={"data": []}), []), "u_0000")


def test_mixpanel_problems_are_app_errors():
    with pytest.raises(AppError, match="service account"):
        MixpanelClient("token").feature_rows("2026-09-01", "2026-09-13")
    with pytest.raises(AppError, match="Unknown MIXPANEL_REGION"):
        MixpanelClient("token", "1", "user", "secret", region="mars").feature_rows("2026-09-01", "2026-09-13")
