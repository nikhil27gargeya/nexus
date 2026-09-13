"""Run the agent on every fixture scenario and grade the outcome against the scenario's expected verdict.

The pass bar is zero false remediations: an unproven admission of fault is the failure that matters most.
"""

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from nexus import pipeline
from nexus.apps.fixtures import iter_scenarios
from nexus.config import REPO_ROOT, Settings, settings

RESULTS = REPO_ROOT / "evals" / "results"
PROMPT = Path(pipeline.__file__).parent / "agent" / "prompts" / "agent.md"


@dataclass
class EvalRow:
    scenario: str
    run: int
    expected_claim: str
    claim: str | None
    confidence: float | None
    expected_outcome: str
    outcome: str
    stopped_at: int | None
    steps: int
    seconds: float
    correct: bool
    false_remediation: bool
    error: str | None


def run_evals(runs: int = 1, fake: bool = False, cfg: Settings = settings) -> list[EvalRow]:
    rows = []
    for scenario, data in iter_scenarios(cfg.fixtures_dir):
        expected_claim = data.get("expected", "no_harm")
        expected_outcome = "remediation" if expected_claim == "causal_harm" else "standard_offer"
        for n in range(1, runs + 1):
            started = time.monotonic()
            events = list(pipeline.run(scenario, fake=fake, cfg=cfg))
            verdict = next((e.data for e in events if e.type == "verdict"), {})
            decision = next((e.data for e in events if e.type == "decision"), {})
            outcome = events[-1].data.get("outcome", "standard_offer")
            rows.append(EvalRow(
                scenario=scenario, run=n, expected_claim=expected_claim, claim=verdict.get("claim"),
                confidence=verdict.get("confidence"), expected_outcome=expected_outcome, outcome=outcome,
                stopped_at=decision.get("stopped_at"), steps=sum(e.type == "step" for e in events),
                seconds=round(time.monotonic() - started, 1), correct=outcome == expected_outcome,
                false_remediation=outcome == "remediation" and expected_outcome != "remediation",
                error=next((e.data["message"] for e in events if e.type in ("run.error", "agent.error")), None),
            ))
    return rows


def write_report(rows: list[EvalRow], model: str, results_dir: Path = RESULTS) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    prompt_version = hashlib.sha1(PROMPT.read_bytes()).hexdigest()[:8]
    correct = sum(r.correct for r in rows)
    false_remediations = sum(r.false_remediation for r in rows)
    missed = sum(r.expected_outcome == "remediation" and r.outcome != "remediation" for r in rows)
    (results_dir / f"run_{stamp}.json").write_text(json.dumps(
        {"model": model, "prompt_version": prompt_version, "at": stamp, "rows": [asdict(r) for r in rows]}, indent=2))
    lines = [
        "# Eval results",
        "",
        f"Model `{model}` · prompt `{prompt_version}` · {stamp} · fixture data",
        "",
        f"**{correct}/{len(rows)} correct · {false_remediations} false remediations · {missed} missed remediations**",
        "",
        "| Scenario | Run | Expected | Agent claim | Confidence | Outcome | Stopped at | Steps | Seconds | Result |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        result = "✅" if r.correct else ("❌ false remediation" if r.false_remediation else "⚠️ missed")
        lines.append(f"| {r.scenario} | {r.run} | {r.expected_claim} | {r.claim or '–'} | "
                     f"{r.confidence if r.confidence is not None else '–'} | {r.outcome} | {r.stopped_at or '–'} | "
                     f"{r.steps} | {r.seconds} | {result} |")
    report = results_dir / "latest.md"
    report.write_text("\n".join(lines) + "\n")
    return report
