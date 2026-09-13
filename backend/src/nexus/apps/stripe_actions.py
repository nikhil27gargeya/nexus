"""Stripe writes, triggered only by the customer's click.

A remediation credit is applied only when the recorded run for that customer proved harm and cited the incident.
Every credit and reversal carries an idempotency key, so a double click or a retry never moves money twice.
"""

from dataclasses import dataclass
from functools import partial
from typing import Any

import httpx

from nexus.apps.base import AppError
from nexus.apps.http import ClientFactory, request_json
from nexus.config import Settings

COUPON = "nexus_standard_offer"


def stripe_client(cfg: Settings) -> ClientFactory:
    return partial(httpx.Client, base_url="https://api.stripe.com/v1", timeout=20, auth=(cfg.stripe_secret_key, ""))


@dataclass(frozen=True)
class Subscriber:
    customer: dict[str, Any]
    subscription: dict[str, Any]
    transactions: list[dict[str, Any]]


def find_customer(new_client: ClientFactory, user_id: str) -> dict[str, Any] | None:
    """The Stripe customer tagged with this user_id, or None."""
    found = request_json(new_client, "stripe", "GET", "/customers/search",
                         params={"query": f"metadata['user_id']:'{user_id}'"})["data"]
    return found[0] if found else None


def lookup_subscriber(new_client: ClientFactory, user_id: str) -> Subscriber:
    """The Stripe customer tagged with this user_id, their subscription and their balance transactions."""
    def get(path: str, **params: Any) -> Any:
        return request_json(new_client, "stripe", "GET", path, params=params)

    customer = find_customer(new_client, user_id)
    if customer is None:
        raise AppError(f"stripe: no customer with user_id {user_id}")
    subs = get("/subscriptions", customer=customer["id"], status="all")["data"]
    if not subs:
        raise AppError(f"stripe: {customer['name']} has no subscription")
    transactions = get(f"/customers/{customer['id']}/balance_transactions", limit=100)["data"]
    return Subscriber(customer, subs[0], transactions)


def net_credited_incidents(transactions: list[dict[str, Any]]) -> list[str]:
    """Incidents with a Nexus credit that has not been reversed by `nexus stripe-reset`."""
    metadata = [t.get("metadata") or {} for t in transactions]
    credited = [m["nexus_incident"] for m in metadata if m.get("nexus_incident")]
    reversed_ = [m["nexus_reversal"] for m in metadata if m.get("nexus_reversal")]
    remaining = list(credited)
    for incident in reversed_:
        if incident in remaining:
            remaining.remove(incident)
    return remaining


def _count(transactions: list[dict[str, Any]], key: str, incident: str) -> int:
    return sum(1 for t in transactions if (t.get("metadata") or {}).get(key) == incident)


def _result(kind: str, obj: dict[str, Any], detail: str) -> dict[str, Any]:
    return {"kind": kind, "ok": True, "stripe_id": obj.get("id"), "detail": detail}


class StripeActions:
    def __init__(self, cfg: Settings) -> None:
        self.client = stripe_client(cfg)

    def _send(self, method: str, path: str, idempotency_key: str | None = None, **data: Any) -> dict[str, Any]:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return request_json(self.client, "stripe", method, path, data=data or None, headers=headers)

    def credit(self, user_id: str, amount_usd: int, incident: str) -> dict[str, Any]:
        found = lookup_subscriber(self.client, user_id)
        if incident in net_credited_incidents(found.transactions):
            return {"kind": "credit", "ok": True, "stripe_id": None, "detail": f"already credited for {incident}"}
        resets = _count(found.transactions, "nexus_reversal", incident)
        txn = self._send("POST", f"/customers/{found.customer['id']}/balance_transactions",
                         idempotency_key=f"nexus-credit-{user_id}-{incident}-{resets}",
                         amount=-amount_usd * 100, currency="usd", description=f"Nexus remediation for {incident}",
                         **{"metadata[nexus_incident]": incident})
        return _result("credit", txn, f"${amount_usd} credit for {incident}")

    def standard_offer(self, user_id: str, coupon: str = COUPON) -> dict[str, Any]:
        sub = lookup_subscriber(self.client, user_id).subscription
        if sub.get("discounts"):
            return {"kind": "discount", "ok": True, "stripe_id": sub["id"], "detail": "offer already applied"}
        updated = self._send("POST", f"/subscriptions/{sub['id']}", **{"discounts[0][coupon]": coupon})
        return _result("discount", updated, "standard offer applied to the subscription")

    def schedule_cancel(self, user_id: str) -> dict[str, Any]:
        sub = lookup_subscriber(self.client, user_id).subscription
        if sub.get("cancel_at_period_end"):
            return {"kind": "cancel", "ok": True, "stripe_id": sub["id"], "detail": "already set to cancel"}
        updated = self._send("POST", f"/subscriptions/{sub['id']}", cancel_at_period_end="true")
        return _result("cancel", updated, "cancels at the end of the billing period")

    def reset(self, user_id: str) -> list[str]:
        found = lookup_subscriber(self.client, user_id)
        customer, sub, txns = found.customer, found.subscription, found.transactions
        done = []
        if sub.get("cancel_at_period_end"):
            self._send("POST", f"/subscriptions/{sub['id']}", cancel_at_period_end="false")
            done.append("cancellation removed")
        if sub.get("discounts"):
            self._send("DELETE", f"/subscriptions/{sub['id']}/discount")
            done.append("discount removed")
        for incident in net_credited_incidents(txns):
            amount = next(t["amount"] for t in txns if (t.get("metadata") or {}).get("nexus_incident") == incident)
            self._send("POST", f"/customers/{customer['id']}/balance_transactions",
                       idempotency_key=f"nexus-reset-{user_id}-{incident}-{_count(txns, 'nexus_reversal', incident)}",
                       amount=-amount, currency="usd", description=f"Reset of Nexus remediation for {incident}",
                       **{"metadata[nexus_reversal]": incident})
            done.append(f"credit for {incident} reversed")
        return done or ["nothing to reset"]
