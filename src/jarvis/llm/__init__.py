from __future__ import annotations

from jarvis.core.config import LLMConfig
from jarvis.core.config import LLMProvider as ProviderName
from jarvis.core.secrets import SecretName, get_secret
from jarvis.llm.base import LLMError, LLMProvider


def create_provider(cfg: LLMConfig) -> LLMProvider:
    if cfg.provider is ProviderName.OLLAMA:
        from jarvis.llm.ollama_provider import OllamaProvider

        return OllamaProvider(cfg.ollama)

    from jarvis.llm.anthropic_provider import AnthropicProvider

    key = get_secret(SecretName.ANTHROPIC_API_KEY)
    if key is None:
        raise LLMError("no Anthropic API key - run `jarvis secret set ANTHROPIC_API_KEY`")
    return AnthropicProvider(cfg.anthropic, key)


__all__ = ["LLMError", "LLMProvider", "create_provider"]
