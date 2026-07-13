"""Optional frontier provider. Only used when config routes a task to
"anthropic"; requires `uv sync --extra anthropic` and ANTHROPIC_API_KEY.
Embeddings always stay local (Anthropic doesn't offer an embeddings API)."""

from __future__ import annotations

from typing import Any

from applyr.llm.provider import ChatMessage, ChatResponse, ProviderError, ToolCallRequest


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str = "claude-sonnet-5") -> None:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover
            raise ProviderError(
                "anthropic extra not installed — run `uv sync --extra anthropic`"
            ) from exc
        self._client = anthropic.Anthropic()
        self.model = model

    def _split(self, messages: list[ChatMessage]) -> tuple[str, list[dict[str, str]]]:
        system = "\n".join(m.content for m in messages if m.role == "system")
        rest = [
            {"role": m.role, "content": m.content} for m in messages if m.role != "system"
        ]
        return system, rest

    def chat(self, messages: list[ChatMessage]) -> str:
        system, rest = self._split(messages)
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=system or "You are a concise assistant.",
            messages=rest,
        )
        return "".join(
            block.text for block in resp.content if getattr(block, "type", "") == "text"
        )

    def chat_with_tools(
        self, messages: list[ChatMessage], tools: list[dict[str, Any]]
    ) -> ChatResponse:
        system, rest = self._split(messages)
        anthropic_tools = [
            {
                "name": t["function"]["name"],
                "description": t["function"].get("description", ""),
                "input_schema": t["function"]["parameters"],
            }
            for t in tools
        ]
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=system or "You extract structured actions from user input.",
            messages=rest,
            tools=anthropic_tools,
        )
        text_parts: list[str] = []
        calls: list[ToolCallRequest] = []
        for block in resp.content:
            block_type = getattr(block, "type", "")
            if block_type == "text":
                text_parts.append(block.text)
            elif block_type == "tool_use":
                calls.append(
                    ToolCallRequest(name=block.name, arguments=dict(block.input))
                )
        return ChatResponse(text="".join(text_parts) or None, tool_calls=calls)

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise ProviderError("embeddings are always local — use the Ollama provider")
