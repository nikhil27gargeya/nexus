import json

import pytest
from fastapi.testclient import TestClient

from nexus.api import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "REPLAYS", tmp_path)
    monkeypatch.setattr(main, "EXAMPLES", tmp_path / "no-examples")
    monkeypatch.setattr(main, "PACE", {})
    return TestClient(main.app)


def _frames(text):
    frames = []
    for block in text.replace("\r\n", "\n").strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        frames.append((lines.get("event"), json.loads(lines.get("data", "null"))))
    return frames


def test_stream_runs_the_agent_then_replays_the_recording(client, tmp_path):
    url = "/api/runs/stream?scenario=fathom&data=fixtures&agent=fake"
    first = _frames(client.get(url).text)
    kinds = [kind for kind, _ in first]
    assert kinds[0] == "meta" and first[0][1]["replayed"] is False
    assert {"run.started", "step", "gate", "decision", "remediation", "run.done"} <= set(kinds)
    assert any(d.get("view", {}).get("big") == "31" for k, d in first if k == "step")
    assert (tmp_path / "fathom.fixtures.fake.jsonl").exists()

    second = _frames(client.get(url).text)
    assert second[0][1]["replayed"] is True
    assert [k for k, _ in second[1:]] == kinds[1:]


def test_app_pages_show_the_latest_recorded_run(client):
    assert client.get("/api/runs/latest?scenario=fathom").status_code == 404
    client.get("/api/runs/stream?scenario=fathom&data=fixtures&agent=fake")
    events = client.get("/api/runs/latest?scenario=fathom").json()
    assert any(e["type"] == "step" and e["data"]["app"] == "mixpanel" for e in events)
    page = client.get("/apps/mixpanel?scenario=fathom")
    assert page.status_code == 200 and "What it means" in page.text
    assert client.get("/apps/stripe").status_code == 422


def test_a_fresh_clone_replays_the_committed_examples(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "REPLAYS", tmp_path / "no-replays")
    monkeypatch.setattr(main, "PACE", {})
    client = TestClient(main.app)
    frames = _frames(client.get("/api/runs/stream?scenario=fathom").text)
    assert frames[0][1]["replayed"] is True
    assert ("remediation" in [k for k, _ in frames]) and not (tmp_path / "no-replays").exists()
    quanta = client.get("/api/runs/latest?scenario=quanta").json()
    assert next(e["data"]["outcome"] for e in quanta if e["type"] == "run.done") == "standard_offer"
    assert "MERGELINE-SYNC-1" in main._proven_incidents("fathom")


def test_committed_example_runs_hold_no_stripe_customer_ids():
    for path in main.EXAMPLES.glob("*/events.jsonl"):
        assert "cus_" not in path.read_text()


def test_unknown_scenario_is_a_404(client):
    assert client.get("/api/runs/stream?scenario=nope").status_code == 404


def test_scenarios_and_business_config(client):
    names = {s["key"]: s["name"] for s in client.get("/api/scenarios").json()}
    assert names["fathom"] == "Fathom" and names["quanta"] == "Quanta"
    assert client.get("/api/business").json()["standard_offer"]["percent_off"] == 20
