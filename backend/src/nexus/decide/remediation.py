from datetime import date

from nexus.agent.board import Board
from nexus.config import RemediationConfig, StandardOfferConfig, feature_label
from nexus.dates import date_part, parse_datetime
from nexus.decide.gates import feature_of, issue_day
from nexus.models import Account, Evidence, ProofLine, Remediation, StandardOffer, Verdict

MONTHS = {1: "one month", 2: "two months", 3: "three months"}


def _format_day(d: date) -> str:
    return f"{d:%b} {d.day}"


def _format_hours(start: str, end: str) -> str:
    hours = (parse_datetime(end) - parse_datetime(start)).total_seconds() / 3600
    whole = int(hours)
    if abs(hours - whole - 0.5) < 0.01:
        return f"{whole}½"
    return str(whole) if hours == whole else f"{hours:.1f}"


def _is_sync(issue: Evidence) -> bool:
    return "Sync" in issue.data.get("type", "")


def build_remediation(verdict: Verdict, board: Board, account: Account, cfg: RemediationConfig,
                      features: dict[str, str] | None = None) -> Remediation:
    """The customer-facing remediation, built only from evidence the agent cited and the gates accepted."""
    features = features or {}
    cited = [e for i in verdict.harm_ids if (e := board.get(i))]
    incident = next((e for e in cited if e.kind == "incident"), None)
    issues = sorted((e for e in cited if e.kind == "sentry_issue"), key=lambda e: -e.data["user_count"])
    issue = issues[0] if issues else None
    usage = next((e for i in verdict.usage_ids if (e := board.get(i)) and e.kind == "feature_usage"), None)
    active = usage.data.get("active_members_on_day", 0) if usage else 0

    sentences: list[str] = []
    proof: list[ProofLine] = []
    if incident:
        hours = _format_hours(incident.data["start"], incident.data["end"])
        started = _format_day(date_part(incident.data["start"]))
        sentences.append(f"On {started}, Mergeline stopped syncing data between your apps for {hours} hours.")
        down = f"{started} · data syncing was down for {hours} hours in your region"
        proof.append(ProofLine(app="datadog", text=down))
    if issue:
        peak = issue_day(issue)
        when = "that day" if incident and peak == date_part(incident.data["start"]) else f"on {_format_day(peak)}"
        working = f", while {active} of your teammates were working" if active else ""
        count = issue.data["user_count"]
        if _is_sync(issue):
            sentences.append(f"{count} of your syncs failed {when}{working}.")
            failed = f"{_format_day(peak)} · {count} data syncs failed for your workspace"
            proof.append(ProofLine(app="sentry", text=failed))
        else:
            sentences.append(f"Your workspace hit {count} errors {when}{working}.")
            proof.append(ProofLine(app="sentry", text=f"{_format_day(peak)} · {count} errors in your workspace"))

    feature = feature_of(issue, features) if issue else None
    share = usage.data.get("share_by_feature", {}).get(feature, 0.0) if usage and feature else 0.0
    if share >= 0.5:
        what = "Keeping your apps in sync" if feature == "sync" else feature_label(feature).capitalize()
        sentences.append(f"{what} is what your team uses Mergeline for most, so that's on us.")
        usage_line = (f"{round(share * 100)}% of your team's use of Mergeline is {feature_label(feature)} · "
                      f"{active} teammates were working that day")
        proof.append(ProofLine(app="mixpanel", text=usage_line))
    else:
        sentences.append("That's on us.")

    fixed_line = None
    fixed = next((e for e in issues if e.data.get("resolved_at") and e.data.get("events_since_fix") == 0), None)
    if fixed:
        fixed_line = f"Fixed on {_format_day(date_part(fixed.data['resolved_at']))}. No sync failures since."

    credit_usd = cfg.credit_months * account.price_usd
    if any(e.id in account.prior_credit_incidents for e in cited):
        credit_usd = 0
        credit_line = "You were already credited for this. We still wanted you to hear it from us."
    else:
        months = MONTHS.get(cfg.credit_months, f"{cfg.credit_months} months")
        credit_line = f"We've added a ${credit_usd} credit ({months} of {account.plan}), whatever you decide."

    return Remediation(
        headline=cfg.headline, letter=" ".join(sentences), proof=proof, fixed_line=fixed_line,
        credit_line=credit_line, credit_usd=credit_usd,
        accept_label=cfg.accept_label.format(plan=account.plan), decline_label=cfg.decline_label,
    )


def build_standard_offer(account: Account, cfg: StandardOfferConfig) -> StandardOffer:
    """The business's normal retention offer. Makes no claim about what went wrong."""
    if cfg.kind == "none":
        until = f"{_format_day(account.renews)}, {account.renews.year}"
        return StandardOffer(kind="none", headline=f"Cancel your {account.plan} plan?",
                             body=f"You keep access until {until}.",
                             accept_label=f"Keep {account.plan}", decline_label=cfg.decline_label)
    values = {"plan": account.plan, "percent_off": cfg.percent_off, "months": cfg.months}
    return StandardOffer(
        kind=cfg.kind, headline=cfg.headline.format(**values), body=cfg.body.format(**values),
        accept_label=cfg.accept_label.format(**values), decline_label=cfg.decline_label.format(**values),
        percent_off=cfg.percent_off if cfg.kind == "percent_off" else None, months=cfg.months,
    )
