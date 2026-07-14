"""LLM provider abstraction. Ollama is the default; a frontier model can be
routed in per task via config. Core never imports this package."""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: str  # system | user | assistant | tool
    content: str


class ToolCallRequest(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ChatResponse(BaseModel):
    text: str | None = None
    tool_calls: list[ToolCallRequest] = Field(default_factory=list)


class ProviderError(Exception):
    pass


class LLMProvider(Protocol):
    name: str

    def chat(self, messages: list[ChatMessage]) -> str: ...

    def chat_with_tools(
        self, messages: list[ChatMessage], tools: list[dict[str, Any]]
    ) -> ChatResponse: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...
