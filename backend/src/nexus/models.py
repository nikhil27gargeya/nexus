from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

App = Literal["sentry", "datadog", "mixpanel", "stripe"]
Claim = Literal["causal_harm", "no_harm", "insufficient_evidence"]
Outcome = Literal["remediation", "standard_offer"]


class Account(BaseModel):
    user_id: str
    name: str
    region: str
    seats: int
    plan: str = "Pro"
    price_usd: int = 30
    renews: date
    stripe_customer_id: str | None = None
    prior_credit_incidents: list[str] = Field(default_factory=list)


class Evidence(BaseModel):
    id: str
    app: App
    kind: str
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)


class Step(BaseModel):
    n: int
    tool: str
    app: App | None
    args: dict[str, Any]
    ok: bool
    evidence_ids: list[str]
    summary: str
    ms: int
    view: dict[str, Any] = Field(default_factory=dict)


class Verdict(BaseModel):
    claim: Claim = Field(description="causal_harm only if our failure plausibly caused the cancellation")
    harm_ids: list[str] = Field(default_factory=list, description="Evidence IDs showing harm to this customer")
    usage_ids: list[str] = Field(default_factory=list, description="Evidence IDs showing the usage change")
    confidence: float = Field(ge=0, le=1, description="How strongly the evidence you observed supports the claim")
    reasoning: str = Field(description="Two or three sentences, citing evidence IDs")


class GateResult(BaseModel):
    n: int
    name: str
    status: Literal["pass", "fail", "skipped"]
    reason: str


class Decision(BaseModel):
    remediate: bool
    claim: Claim
    gates: list[GateResult]
    stopped_at: int | None


class ProofLine(BaseModel):
    app: App
    text: str


class Remediation(BaseModel):
    headline: str
    letter: str
    proof: list[ProofLine]
    fixed_line: str | None
    credit_line: str
    credit_usd: int
    accept_label: str
    decline_label: str


class StandardOffer(BaseModel):
    kind: Literal["percent_off", "pause", "none"]
    headline: str
    body: str
    accept_label: str
    decline_label: str
    percent_off: int | None = None
    months: int | None = None


class NexusEvent(BaseModel):
    type: str
    data: dict[str, Any] = Field(default_factory=dict)
