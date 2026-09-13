import json

from nexus import verify as verify_module
from nexus.evals import run_evals, write_report


def test_scripted_agent_passes_every_scenario_with_no_false_remediation(tmp_path):
    rows = run_evals(fake=True)
    assert rows and all(r.correct for r in rows)
    assert not any(r.false_remediation for r in rows)
    report = write_report(rows, "fake", results_dir=tmp_path)
    assert "0 false remediations" in report.read_text()
    assert list(tmp_path.glob("run_*.json"))


def _recording(tmp_path):
    events = [
        {"type": "run.started", "data": {"account": {"user_id": "u_4812"}, "data": "live",
                                         "window": {"since": "2026-07-19", "until": "2026-09-13"}}},
        {"type": "step", "data": {"evidence_ids": ["MERGELINE-SYNC-1"], "view": {"big": "31"}}},
        {"type": "step", "data": {"evidence_ids": ["INC-2"], "view": {"line": "Sync · 2026-09-03 09:10–14:40 UTC"}}},
        {"type": "step", "data": {"evidence_ids": ["MXP-u_4812-2026-09-03"], "view": {"big": "82%"}}},
        {"type": "verdict", "data": {"harm_ids": ["MERGELINE-SYNC-1", "INC-2"],
                                     "usage_ids": ["MXP-u_4812-2026-09-03"]}},
    ]
    path = tmp_path / "fathom.live.claude.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in events))
    return path


class LiveStub:
    def __init__(self, cfg, user_id, count=31):
        self.count = count

    def sentry_get_issue(self, issue_id, user_id):
        return {"type": "SyncTimeoutError", "user_count": self.count}

    def datadog_get_incident(self, incident_id):
        return {"title": "Sync jobs timing out", "start": "2026-09-03T09:10:00Z"}

    def mixpanel_feature_usage(self, user_id, since, until, day):
        return {"share_by_feature": {"sync": 0.82, "reports": 0.1}, "active_members_on_day": 4}


def test_verify_confirms_every_citation_against_the_apps(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.apps.live.LiveApps", LiveStub)
    lines, ok = verify_module.verify_recording(_recording(tmp_path))
    assert ok and lines[-1] == "verified 3/3"


def test_verify_flags_evidence_that_changed(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.apps.live.LiveApps", lambda cfg, user: LiveStub(cfg, user, count=12))
    lines, ok = verify_module.verify_recording(_recording(tmp_path))
    assert not ok and lines[0].startswith("✗ MERGELINE-SYNC-1")
