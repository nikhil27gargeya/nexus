from datetime import date

from nexus.agent.board import Board
from nexus.config import GateConfig, feature_label
from nexus.dates import date_part
from nexus.models import Account, Decision, Evidence, GateResult, Verdict

GATE_NAMES = [
    "Every claim points to real data",
    "This customer hit the failure",
    "It was a confirmed outage in their region",
    "It broke something they depend on",
    "The evidence is strong enough",
]

Check = tuple[bool, str]


def _format_day(d: date) -> str:
    return f"{d:%b} {d.day}"


def issue_day(issue: Evidence) -> date:
    return date_part(issue.data.get("peak_date") or issue.data["first_seen"])


def feature_of(evidence: Evidence, features: dict[str, str]) -> str | None:
    return features.get(evidence.data.get("type", "")) or features.get(evidence.data.get("title", ""))


def cited(board: Board, ids: list[str], kind: str) -> list[Evidence]:
    return [e for i in ids if (e := board.get(i)) and e.kind == kind]


def _customer_hit(issues: list[Evidence], window_start: date, cancel: date,
                  cfg: GateConfig) -> tuple[list[Evidence], Check]:
    """Sentry: errors on this customer's own account, inside the window."""
    hit = sorted((e for e in issues if e.data.get("user_count", 0) >= cfg.min_errors
                  and window_start <= issue_day(e) <= cancel), key=lambda e: -e.data["user_count"])
    if hit:
        top = hit[0]
        return hit, (True, f"{top.data['user_count']} {top.data['type']} errors on {_format_day(issue_day(top))}")
    if issues:
        return [], (False, f"Only {max(e.data.get('user_count', 0) for e in issues)} errors on their account")
    return [], (False, "No errors on their account")


def _confirmed_outage(incidents: list[Evidence], hit: list[Evidence], account: Account, window_start: date,
                      cancel: date, cfg: GateConfig) -> tuple[Evidence | None, Evidence | None, Check]:
    """Datadog: a real incident in the customer's region, at the time their errors happened."""
    if not hit:
        return None, None, (False, "No customer errors to match")
    check: Check = (False, "No outage cited")
    for incident in incidents:
        start = date_part(incident.data["start"])
        if incident.data["region"] != account.region:
            check = (False, f"Outage was in {incident.data['region']}, customer is in {account.region}")
            continue
        if not window_start <= start <= cancel:
            check = (False, "Outage was outside the investigation window")
            continue
        near = [e for e in hit if abs((issue_day(e) - start).days) <= cfg.max_days_from_outage]
        if not near:
            gap = min(abs((issue_day(e) - start).days) for e in hit)
            check = (False, f"Errors were {gap} days away from the outage")
            continue
        return incident, near[0], (True, f"{incident.id} in {incident.data['region']} on {_format_day(start)}")
    return None, None, check


def _depends_on_it(usage: list[Evidence], outage: Evidence | None, issue: Evidence | None, account: Account,
                   cfg: GateConfig, features: dict[str, str]) -> Check:
    """Mixpanel: the broken feature is a large share of what this team does, and they were working that day."""
    if outage is None or issue is None:
        return False, "No outage to weigh"
    if not usage:
        return False, "No usage evidence cited"
    feature = feature_of(issue, features) or feature_of(outage, features)
    if feature is None:
        return False, f"No product feature mapped for {issue.data.get('type')}"
    data, day = usage[0].data, issue_day(issue).isoformat()
    share = data.get("share_by_feature", {}).get(feature, 0.0)
    active = data.get("active_members_on_day", 0)
    pct = round(share * 100)
    if data.get("day") != day:
        return False, f"Usage was checked for {data.get('day')}, not the day it broke"
    if share < cfg.min_feature_share:
        return False, f"{feature_label(feature)} is only {pct}% of their activity"
    if active < cfg.min_active_members:
        return False, "Nobody on the team was active that day"
    return True, f"{feature_label(feature)} is {pct}% of their activity · {active} of {account.seats} active"


def evaluate(verdict: Verdict, board: Board, account: Account, window_start: date, cancel: date,
             cfg: GateConfig, features: dict[str, str] | None = None) -> Decision:
    features = features or {}
    ids = verdict.harm_ids + verdict.usage_ids
    missing = [i for i in ids if i not in board]
    real = (not missing, "Nothing cited" if not ids else
            f"{len(ids)}/{len(ids)} verified" if not missing else f"{missing[0]} was never fetched")

    hit, customer_hit = _customer_hit(cited(board, verdict.harm_ids, "sentry_issue"), window_start, cancel, cfg)
    outage, issue, confirmed = _confirmed_outage(cited(board, verdict.harm_ids, "incident"), hit, account,
                                                 window_start, cancel, cfg)
    depends = _depends_on_it(cited(board, verdict.usage_ids, "feature_usage"), outage, issue, account, cfg, features)
    strong = (verdict.confidence >= cfg.min_confidence, f"{verdict.confidence:.2f} / {cfg.min_confidence:.2f}")

    gates: list[GateResult] = []
    stopped_at: int | None = None
    for n, (name, (passed, reason)) in enumerate(
            zip(GATE_NAMES, [real, customer_hit, confirmed, depends, strong], strict=True), start=1):
        if stopped_at is not None:
            gates.append(GateResult(n=n, name=name, status="skipped", reason="not needed"))
            continue
        gates.append(GateResult(n=n, name=name, status="pass" if passed else "fail", reason=reason))
        if not passed:
            stopped_at = n

    remediate = stopped_at is None and verdict.claim == "causal_harm"
    claim = "causal_harm" if remediate else ("no_harm" if not hit else "insufficient_evidence")
    return Decision(remediate=remediate, claim=claim, gates=gates, stopped_at=stopped_at)
