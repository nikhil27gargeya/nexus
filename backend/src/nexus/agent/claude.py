from typing import Any

from nexus.agent.loop import ToolUse, Turn


class ClaudeModel:
    def __init__(self, api_key: str, model: str, max_tokens: int) -> None:
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set. Add it to .env, or run with --fake.")
        import anthropic

        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model
        self.max_tokens = max_tokens

    def turn(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Turn:
        response = self.client.messages.create(
            model=self.model, max_tokens=self.max_tokens, system=system, tools=tools, messages=messages
        )
        uses = [ToolUse(id=b.id, name=b.name, input=dict(b.input)) for b in response.content if b.type == "tool_use"]
        text = "".join(b.text for b in response.content if b.type == "text")
        return Turn(text=text, tool_uses=uses, raw=response.content)
