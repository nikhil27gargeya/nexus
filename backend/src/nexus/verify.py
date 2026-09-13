"""Re-check a recorded run against the live apps, through a separate read of each cited evidence ID.

An agent that reports its own evidence proves nothing; this re-reads Sentry, Datadog and Mixpanel directly.
"""

from pathlib import Path
from typing import Any

from nexus.config import Settings, settings
from nexus.models import NexusEvent


def _events(path: Path) -> list[NexusEvent]:
    return [NexusEvent.model_validate_json(line) for line in path.read_text().splitlines() if line.strip()]


def verify_recording(path: Path, cfg: Settings = settings) -> tuple[list[str], bool]:
    from nexus.apps.live import LiveApps

    events = _events(path)
    started = next((e.data for e in events if e.type == "run.started"), None)
    verdict = next((e.data for e in events if e.type == "verdict"), None)
    if not started or not verdict:
        return [f"{path.name}: no completed run to verify"], False
    if started.get("data") != "live":
        return [f"{path.name}: recorded on fixture data; nothing live to re-check"], True

    user = started["account"]["user_id"]
    since, until = started["window"]["since"], started["window"]["until"]
    latest: dict[str, dict[str, Any]] = {}
    for step in (e.data for e in events if e.type == "step"):
        for evidence_id in step.get("evidence_ids", []):
            latest[evidence_id] = step

    apps = LiveApps(cfg, user)
    lines, ok = [], True
    for evidence_id in verdict.get("harm_ids", []) + verdict.get("usage_ids", []):
        view = latest.get(evidence_id, {}).get("view", {})
        try:
            if evidence_id.startswith("INC-"):
                incident = apps.datadog_get_incident(evidence_id)
                recorded = view.get("incident") or next(
                    (i for i in view.get("incidents", []) if i.get("id") == evidence_id), {})
                matched = bool(incident) and (incident["start"] == recorded.get("start")
                                              or incident["start"][:10] in view.get("line", ""))
                detail = f"Datadog {incident['title']} starting {incident['start']}" if incident else "not found"
            elif evidence_id.startswith("MXP-"):
                day = evidence_id[-10:]
                usage = apps.mixpanel_feature_usage(user, since, until, day)
                shares = usage.get("share_by_feature") or {}
                top, share = max(shares.items(), key=lambda kv: kv[1]) if shares else ("none", 0.0)
                matched = f"{round(share * 100)}%" == view.get("big")
                detail = (f"Mixpanel {top} is {round(share * 100)}% of activity, "
                          f"{usage.get('active_members_on_day', 0)} active on {day}")
            else:
                issue = apps.sentry_get_issue(evidence_id, user)
                matched = bool(issue) and str(issue["user_count"]) == view.get("big")
                detail = f"Sentry {issue['type']} ×{issue['user_count']} for {user}" if issue else "not found"
        except Exception as exc:
            matched, detail = False, f"error: {exc}"
        ok &= matched
        lines.append(f"{'✓' if matched else '✗'} {evidence_id}: {detail}")
    lines.append(f"verified {sum(line.startswith('✓') for line in lines)}/{len(lines)}")
    return lines, ok
