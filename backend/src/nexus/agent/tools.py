import json
from dataclasses import dataclass, field
from typing import Any

from nexus.agent.board import Board
from nexus.apps.base import Apps
from nexus.config import feature_label
from nexus.models import App, Evidence, Verdict

SUBMIT = "submit_verdict"

_DATE = {"type": "string", "description": "YYYY-MM-DD"}

SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "sentry_find_user_issues",
        "description": "Sentry: list error issues this customer's user hit between two dates, with the "
        "number of events for this user. Answers: did this customer hit the failure?",
        "input_schema": {
            "type": "object",
            "properties": {"user_id": {"type": "string"}, "since": _DATE, "until": _DATE},
            "required": ["user_id", "since", "until"],
        },
    },
    {
        "name": "sentry_get_issue",
        "description": "Sentry: details for one issue: events per day for this user, the peak day, "
        "and whether it was resolved (release, date, events since the fix).",
        "input_schema": {
            "type": "object",
            "properties": {"issue_id": {"type": "string"}, "user_id": {"type": "string"}},
            "required": ["issue_id", "user_id"],
        },
    },
    {
        "name": "datadog_find_incidents",
        "description": "Datadog: declared incidents between two dates. Pass the customer's region. Answers: was it a "
        "confirmed outage where this customer is?",
        "input_schema": {
            "type": "object",
            "properties": {"region": {"type": "string"}, "since": _DATE, "until": _DATE},
            "required": ["since", "until"],
        },
    },
    {
        "name": "datadog_get_incident",
        "description": "Datadog: one incident's timeline: start, end, severity, region.",
        "input_schema": {
            "type": "object",
            "properties": {"incident_id": {"type": "string"}},
            "required": ["incident_id"],
        },
    },
    {
        "name": "mixpanel_feature_usage",
        "description": "Mixpanel: how this customer's team uses the product: each feature's share of their activity "
        "over the window, and how many teammates were active on `day` (pass the day the errors peaked). "
        "Answers: did the failure break something they depend on? Required before submitting.",
        "input_schema": {
            "type": "object",
            "properties": {"user_id": {"type": "string"}, "since": _DATE, "until": _DATE, "day": _DATE},
            "required": ["user_id", "since", "until", "day"],
        },
    },
    {
        "name": "try_to_disprove",
        "description": "State the story you currently believe. Returns the questions you must answer to "
        "rule out coincidence. Required before submitting.",
        "input_schema": {
            "type": "object",
            "properties": {"claim_summary": {"type": "string"}},
            "required": ["claim_summary"],
        },
    },
    {
        "name": SUBMIT,
        "description": "Submit your final verdict. Cite only evidence IDs that tools returned.",
        "input_schema": Verdict.model_json_schema(),
    },
]

APP_OF_TOOL: dict[str, App] = {
    "sentry_find_user_issues": "sentry",
    "sentry_get_issue": "sentry",
    "datadog_find_incidents": "datadog",
    "datadog_get_incident": "datadog",
    "mixpanel_feature_usage": "mixpanel",
}

DISPROVE_QUESTIONS = [
    "Did these errors hit this customer's own account, and are there enough of them to matter?",
    "Is the outage in the customer's region, and did their errors happen during it?",
    "Is the broken feature a large part of how this team uses the product, or something they rarely touch?",
    "Was anyone on their team actually working the day it broke?",
    "Does anything in the evidence point to a different reason for leaving?",
]


@dataclass
class ToolOutput:
    content: str
    ok: bool = True
    evidence_ids: list[str] = field(default_factory=list)
    summary: str = ""
    view: dict[str, Any] = field(default_factory=dict)


class Toolbox:
    def __init__(self, apps: Apps, board: Board) -> None:
        self.apps = apps
        self.board = board

    def schemas(self) -> list[dict[str, Any]]:
        return SCHEMAS

    def call(self, name: str, args: dict[str, Any]) -> ToolOutput:
        handler = getattr(self, f"_{name}", None)
        if handler is None:
            return ToolOutput(f"error: unknown tool {name}", ok=False, summary="unknown tool")
        try:
            return handler(args)
        except Exception as exc:
            return ToolOutput(f"error: {exc}", ok=False, summary=f"failed: {exc}")

    def _sentry_find_user_issues(self, a: dict[str, Any]) -> ToolOutput:
        issues = self.apps.sentry_find_user_issues(a["user_id"], a["since"], a["until"])
        ids = []
        for issue in issues:
            self.board.add(Evidence(id=issue["id"], app="sentry", kind="sentry_issue",
                                    summary=f"{issue['type']} ×{issue['user_count']}", data=issue))
            ids.append(issue["id"])
        summary = ", ".join(f"{i['type']} ×{i['user_count']}" for i in issues) or "no issues for this user"
        top = max(issues, key=lambda i: i["user_count"], default=None)
        view = {"big": str(top["user_count"]) if top else "0",
                "line": f"{top['type']} · {top['first_seen']}" if top else "errors on this account",
                "evidence": " · ".join(ids) or "No Sentry evidence",
                "issues": [{"id": i["id"], "type": i["type"], "title": i["title"], "count": i["user_count"],
                            "first_seen": i["first_seen"]} for i in issues]}
        return ToolOutput(json.dumps({"issues": issues}), evidence_ids=ids, summary=summary, view=view)

    def _sentry_get_issue(self, a: dict[str, Any]) -> ToolOutput:
        issue = self.apps.sentry_get_issue(a["issue_id"], a["user_id"])
        if issue is None:
            return ToolOutput(f"error: no issue {a['issue_id']}", ok=False, summary="not found")
        self.board.add(Evidence(id=issue["id"], app="sentry", kind="sentry_issue",
                                summary=f"{issue['type']} ×{issue['user_count']}", data=issue))
        fixed = f", fixed in {issue['resolved_in_release']}" if issue.get("resolved_in_release") else ""
        summary = f"{issue['user_count']} events, peak {issue.get('peak_date', '?')}{fixed}"
        line = f"{issue['type']} · peak {issue.get('peak_date', '?')}{fixed}"
        view = {"big": str(issue["user_count"]), "line": line, "evidence": issue["id"],
                "issue": {"id": issue["id"], "type": issue["type"], "title": issue["title"],
                          "count": issue["user_count"], "peak": issue.get("peak_date"), "daily": issue.get("daily"),
                          "release": issue.get("resolved_in_release"), "since_fix": issue.get("events_since_fix")}}
        return ToolOutput(json.dumps(issue), evidence_ids=[issue["id"]], summary=summary, view=view)

    def _datadog_find_incidents(self, a: dict[str, Any]) -> ToolOutput:
        incidents = self.apps.datadog_find_incidents(a.get("region"), a["since"], a["until"])
        ids = []
        for inc in incidents:
            self.board.add(Evidence(id=inc["id"], app="datadog", kind="incident",
                                    summary=f"{inc['title']} ({inc['region']})", data=inc))
            ids.append(inc["id"])
        summary = ", ".join(f"{i['id']} {i['title']}" for i in incidents) or "no incidents"
        line = "outage in their region" if incidents else "outages in their region"
        view = {"big": str(len(incidents)), "line": line, "evidence": " · ".join(ids) or "none in region",
                "region": a.get("region"), "incidents": incidents}
        return ToolOutput(json.dumps({"incidents": incidents}), evidence_ids=ids, summary=summary, view=view)

    def _datadog_get_incident(self, a: dict[str, Any]) -> ToolOutput:
        inc = self.apps.datadog_get_incident(a["incident_id"])
        if inc is None:
            return ToolOutput(f"error: no incident {a['incident_id']}", ok=False, summary="not found")
        self.board.add(Evidence(id=inc["id"], app="datadog", kind="incident",
                                summary=f"{inc['title']} ({inc['region']})", data=inc))
        window = f"{inc['start'][:10]} {inc['start'][11:16]}–{inc['end'][11:16]} UTC"
        return ToolOutput(json.dumps(inc), evidence_ids=[inc["id"]],
                          summary=f"{inc['start'][:16]} → {inc['end'][11:16]} UTC, {inc['region']}",
                          view={"big": "1", "line": f"{inc['title']} · {window}", "evidence": inc["id"],
                                "incident": {k: inc.get(k) for k in ("id", "title", "region", "severity", "start",
                                                                     "end")}})

    def _mixpanel_feature_usage(self, a: dict[str, Any]) -> ToolOutput:
        usage = self.apps.mixpanel_feature_usage(a["user_id"], a["since"], a["until"], a["day"])
        shares = usage.get("share_by_feature") or {}
        if not shares:
            return ToolOutput(json.dumps({"error": "no activity recorded for this customer"}), ok=False,
                              summary="no activity recorded")
        evidence_id = f"MXP-{a['user_id']}-{a['day']}"
        data = {**usage, "day": a["day"]}
        top, share = max(shares.items(), key=lambda kv: kv[1])
        top = feature_label(top)
        pct, active = round(share * 100), usage.get("active_members_on_day", 0)
        self.board.add(Evidence(id=evidence_id, app="mixpanel", kind="feature_usage",
                                summary=f"{top} {pct}% of activity", data=data))
        self.board.flags.add("usage")
        summary = f"{top} is {pct}% of their activity, {active} teammates active on {a['day']}"
        view = {"big": f"{pct}%", "line": f"of their activity is {top} · {active} active on {a['day']}",
                "evidence": evidence_id, "share_by_feature": shares, "active": active, "day": a["day"]}
        return ToolOutput(json.dumps({"evidence_id": evidence_id, **data}), evidence_ids=[evidence_id],
                          summary=summary, view=view)

    def _try_to_disprove(self, a: dict[str, Any]) -> ToolOutput:
        self.board.flags.add("disproved")
        return ToolOutput(json.dumps({"answer_these_before_submitting": DISPROVE_QUESTIONS}),
                          summary="self-check questions returned")
