"""Claude via the official Anthropic SDK.

- Streams every request (large ``max_tokens`` without HTTP timeouts) and reads the
  final message with ``get_final_message()``.
- Adaptive thinking is always on for Claude Opus 5.5; depth is set with ``effort``.
- History is append-only: assistant ``content`` is replayed unchanged, which keeps
  thinking blocks valid and the prompt cache warm.
- Server-side refusal fallback (``fallbacks="default"``) is on by default.
"""

from __future__ import annotations

import logging
from typing import Any

import anthropic
from anthropic.types.beta import BetaMessage, BetaMessageParam, BetaToolParam

from jarvis.core.config import AnthropicConfig
from jarvis.llm.base import (
    LLMError,
    StopKind,
    ToolCall,
    ToolOutcome,
    ToolSpec,
    TurnResult,
    Usage,
)

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"

_STOP_MAP = {
    "end_turn": StopKind.END,
    "stop_sequence": StopKind.END,
    "tool_use": StopKind.TOOL_USE,
    "max_tokens": StopKind.MAX_TOKENS,
    "refusal": StopKind.REFUSAL,
    "pause_turn": StopKind.PAUSE,
}


class AnthropicConversation:
    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        cfg: AnthropicConfig,
        system: str,
        tools: list[ToolSpec],
    ) -> None:
        self._client = client
        self._cfg = cfg
        # Stable prefix (tools -> system) is cached; volatile context goes in user turns.
        self._system: list[dict[str, Any]] = [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
        ]
        self._tools: list[BetaToolParam] = [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
                # Inputs stream as generated; the registry validates them before running.
                "eager_input_streaming": True,
            }
            for t in tools
        ]
        self.messages: list[BetaMessageParam] = []

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def add_tool_results(self, results: list[ToolOutcome]) -> None:
        # All results for one assistant turn go back in a single user message.
        self.messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": r.call_id,
                        "content": r.content,
                        "is_error": r.is_error,
                    }
                    for r in results
                ],
            }
        )

    async def step(self) -> TurnResult:
        kwargs: dict[str, Any] = {
            "model": self._cfg.model,
            "max_tokens": self._cfg.max_tokens,
            "system": self._system,
            "tools": self._tools,
            "messages": self.messages,
            "output_config": {"effort": self._cfg.effort.value},
            # Cache the growing conversation prefix between agent turns.
            "cache_control": {"type": "ephemeral"},
        }
        if self._cfg.server_fallback:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        try:
            async with self._client.beta.messages.stream(**kwargs) as stream:
                message: BetaMessage = await stream.get_final_message()
        except ValueError as exc:
            # Eager input streaming: the SDK couldn't parse a tool input at all.
            raise LLMError(f"model produced unparseable tool input: {exc}", retryable=True) from exc
        except anthropic.AuthenticationError as exc:
            raise LLMError("Anthropic API key was rejected - run `jarvis secret set`") from exc
        except anthropic.PermissionDeniedError as exc:
            raise LLMError(f"Anthropic API permission denied: {exc.message}") from exc
        except anthropic.NotFoundError as exc:
            raise LLMError(f"unknown model {self._cfg.model!r}: {exc.message}") from exc
        except anthropic.BadRequestError as exc:
            raise LLMError(f"Anthropic rejected the request: {exc.message}") from exc
        except anthropic.RateLimitError as exc:
            raise LLMError(
                "Anthropic rate limit reached; try again shortly", retryable=True
            ) from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(
                f"Anthropic API error {exc.status_code}: {exc.message}",
                retryable=exc.status_code >= 500,
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError("cannot reach the Anthropic API", retryable=True) from exc

        stop = _STOP_MAP.get(message.stop_reason or "end_turn", StopKind.END)
        usage = Usage(
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
            cache_read_tokens=message.usage.cache_read_input_tokens or 0,
        )
        if stop is StopKind.REFUSAL:
            # A refused turn may be partial; don't put it into the history.
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            return TurnResult("", [], stop, usage, detail=category)

        self.messages.append({"role": "assistant", "content": message.content})
        text = "".join(b.text for b in message.content if b.type == "text")
        calls = [
            ToolCall(b.id, b.name, b.input if isinstance(b.input, dict) else {})
            for b in message.content
            if b.type == "tool_use"
        ]
        return TurnResult(text, calls, stop, usage)


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, cfg: AnthropicConfig, api_key: str | None) -> None:
        self._cfg = cfg
        self._client = anthropic.AsyncAnthropic(api_key=api_key, timeout=cfg.timeout_s)

    def new_conversation(self, system: str, tools: list[ToolSpec]) -> AnthropicConversation:
        return AnthropicConversation(self._client, self._cfg, system, tools)

    async def check(self) -> tuple[bool, str]:
        try:
            model = await self._client.models.retrieve(self._cfg.model)
        except anthropic.AuthenticationError:
            return False, "API key rejected"
        except anthropic.NotFoundError:
            return False, f"model {self._cfg.model} not available to this key"
        except anthropic.APIError as exc:
            return False, str(exc)
        return True, f"{model.id} reachable"

    async def aclose(self) -> None:
        await self._client.close()
