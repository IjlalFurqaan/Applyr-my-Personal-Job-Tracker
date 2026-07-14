from __future__ import annotations

import json
from typing import Any

import httpx

from jobtrack.llm.provider import ChatMessage, ChatResponse, ProviderError, ToolCallRequest


class OllamaProvider:
    name = "ollama"

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        chat_model: str = "qwen3:8b",
        embed_model: str = "nomic-embed-text",
        timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.chat_model = chat_model
        self.embed_model = embed_model
        self.timeout = timeout

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = httpx.post(f"{self.base_url}{path}", json=payload, timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"cannot reach Ollama at {self.base_url} ({exc}); is `ollama serve` running?"
            ) from exc
        if resp.status_code != 200:
            raise ProviderError(f"Ollama {path} returned {resp.status_code}: {resp.text[:300]}")
        data: dict[str, Any] = resp.json()
        return data

    def chat(self, messages: list[ChatMessage]) -> str:
        data = self._post(
            "/api/chat",
            {
                "model": self.chat_model,
                "messages": [m.model_dump() for m in messages],
                "stream": False,
                # qwen3 emits <think> blocks unless told not to; harmless if
                # the model ignores this option.
                "think": False,
            },
        )
        content = data.get("message", {}).get("content", "")
        return str(content)

    def chat_with_tools(
        self, messages: list[ChatMessage], tools: list[dict[str, Any]]
    ) -> ChatResponse:
        data = self._post(
            "/api/chat",
            {
                "model": self.chat_model,
                "messages": [m.model_dump() for m in messages],
                "tools": tools,
                "stream": False,
                "think": False,
            },
        )
        message = data.get("message", {})
        calls: list[ToolCallRequest] = []
        for call in message.get("tool_calls") or []:
            fn = call.get("function", {})
            arguments = fn.get("arguments", {})
            if isinstance(arguments, str):  # some models return JSON strings
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {}
            calls.append(ToolCallRequest(name=str(fn.get("name", "")), arguments=arguments))
        return ChatResponse(text=message.get("content") or None, tool_calls=calls)

    def embed(self, texts: list[str]) -> list[list[float]]:
        data = self._post("/api/embed", {"model": self.embed_model, "input": texts})
        embeddings = data.get("embeddings")
        if not isinstance(embeddings, list) or len(embeddings) != len(texts):
            raise ProviderError("Ollama /api/embed returned an unexpected payload")
        return [[float(x) for x in vec] for vec in embeddings]
