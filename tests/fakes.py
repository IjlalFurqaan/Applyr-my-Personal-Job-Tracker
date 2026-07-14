from __future__ import annotations

import hashlib
from typing import Any

from applyr.llm.provider import ChatMessage, ChatResponse, ToolCallRequest


class FakeProvider:
    """Deterministic provider for tests: canned chat replies, hashed embeddings."""

    name = "fake"

    def __init__(
        self,
        chat_responses: list[str] | None = None,
        tool_calls: list[ToolCallRequest] | None = None,
        embed_dim: int = 8,
    ) -> None:
        self.chat_responses = list(chat_responses or [])
        self.tool_calls = list(tool_calls or [])
        self.embed_dim = embed_dim
        self.chat_log: list[list[ChatMessage]] = []

    def chat(self, messages: list[ChatMessage]) -> str:
        self.chat_log.append(messages)
        if self.chat_responses:
            return self.chat_responses.pop(0)
        return ""

    def chat_with_tools(
        self, messages: list[ChatMessage], tools: list[dict[str, Any]]
    ) -> ChatResponse:
        self.chat_log.append(messages)
        if self.tool_calls:
            return ChatResponse(tool_calls=[self.tool_calls.pop(0)])
        return ChatResponse(text=self.chat_responses.pop(0) if self.chat_responses else None)

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode()).digest()
            out.append([b / 255.0 for b in digest[: self.embed_dim]])
        return out
