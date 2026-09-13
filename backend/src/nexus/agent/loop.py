import time
from collections.abc import Generator
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import ValidationError

from nexus.agent.tools import APP_OF_TOOL, SUBMIT, Toolbox
from nexus.models import NexusEvent, Step, Verdict

NUDGE = "You did not call a tool. Gather evidence with a tool, or call submit_verdict if you are done."
PREMATURE = ("You called submit_verdict in the same turn as other tools, before seeing their results. "
             "The verdict was discarded. Review the results, then submit again.")
NOT_READY = "Not accepted yet: call mixpanel_feature_usage and try_to_disprove before submit_verdict."
LAST_CHANCE = ("You are almost out of steps. Call submit_verdict now with the evidence you have. "
               "If the evidence is thin, say insufficient_evidence.")


@dataclass
class ToolUse:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class Turn:
    text: str
    tool_uses: list[ToolUse]
    raw: Any = None


class Model(Protocol):
    def turn(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Turn: ...


def _result(tool_use_id: str, content: str, is_error: bool = False) -> dict[str, Any]:
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": content, "is_error": is_error}


def fallback(reason: str) -> Verdict:
    return Verdict(claim="insufficient_evidence", confidence=0.0, reasoning=f"No verdict: {reason}.")


def run_agent(model: Model, toolbox: Toolbox, *, system: str, task: str,
              max_turns: int = 10) -> Generator[NexusEvent, None, Verdict]:
    messages: list[dict[str, Any]] = [{"role": "user", "content": task}]
    step = 0
    for turn_index in range(max_turns):
        try:
            turn = model.turn(system, messages, toolbox.schemas())
        except Exception as exc:
            yield NexusEvent(type="agent.error", data={"message": str(exc)})
            return fallback("the model call failed")
        if turn.text:
            yield NexusEvent(type="thought", data={"text": turn.text})
        messages.append({"role": "assistant", "content": turn.raw})

        if not turn.tool_uses:
            messages.append({"role": "user", "content": NUDGE})
            continue

        results: list[dict[str, Any]] = []
        verdict: Verdict | None = None
        has_other_tools = any(u.name != SUBMIT for u in turn.tool_uses)
        for use in turn.tool_uses:
            if use.name == SUBMIT:
                error = PREMATURE if has_other_tools else None
                if error is None and not {"usage", "disproved"} <= toolbox.board.flags:
                    error = NOT_READY
                if error is None:
                    try:
                        verdict = Verdict.model_validate(use.input)
                    except ValidationError as exc:
                        error = f"Invalid verdict: {exc}"
                results.append(_result(use.id, error or "Verdict received.", is_error=error is not None))
                if error:
                    yield NexusEvent(type="rejected", data={"reason": error})
                continue

            step += 1
            started = time.monotonic()
            output = toolbox.call(use.name, use.input)
            yield NexusEvent(type="step", data=Step(
                n=step, tool=use.name, app=APP_OF_TOOL.get(use.name), args=use.input, ok=output.ok,
                evidence_ids=output.evidence_ids, summary=output.summary, view=output.view,
                ms=round((time.monotonic() - started) * 1000),
            ).model_dump())
            results.append(_result(use.id, output.content, is_error=not output.ok))

        if verdict is not None:
            yield NexusEvent(type="verdict", data=verdict.model_dump())
            return verdict

        content: list[dict[str, Any]] = list(results)
        if turn_index == max_turns - 2:
            content.append({"type": "text", "text": LAST_CHANCE})
        messages.append({"role": "user", "content": content})

    return fallback("step budget exhausted")
