from __future__ import annotations

from applyr.config import Config
from applyr.llm.ollama import OllamaProvider
from applyr.llm.provider import LLMProvider


def local_provider(config: Config) -> OllamaProvider:
    return OllamaProvider(
        base_url=config.llm.base_url,
        chat_model=config.llm.chat_model,
        embed_model=config.llm.embed_model,
    )


def provider_for(config: Config, task: str) -> LLMProvider:
    """task in {classify, jd_parse, prep, draft, say}; defaults to local."""
    target = config.llm.tasks.get(task, "local")
    if target == "anthropic":
        from applyr.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider(model=config.llm.anthropic_model)
    return local_provider(config)
