"""Claude via the official Anthropic SDK.

- Streams every request. Text is forwarded as it's generated (so voice can start
  speaking early) and the final message is read with ``get_final_message()``.
- Adaptive thinking with ``display: "updates"``: between tool calls the model writes
  short progress notes, which arrive as non-empty thinking blocks and are forwarded
  as progress events. Reasoning itself stays hidden.
- History is append-only: assistant ``content`` is replayed unchanged, which keeps
  thinking blocks valid and the prompt cache warm.
- Server-side refusal fallback (``fallbacks="default"``) is on by default.
- Every call is checked against, and recorded in, the daily spend meter.
- A fast model answers conversational turns directly (see :class:`QuickResponder`).
"""

from __future__ import annotations

import base64
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

import anthropic
from anthropic.types.beta import BetaMessage, BetaMessageParam

from jarvis.core.config import AnthropicConfig
from jarvis.llm.base import (
    DeltaSink,
    LLMError,
    StopKind,
    ToolCall,
    ToolOutcome,
    ToolSpec,
    TurnResult,
    Usage,
)
from jarvis.llm.budget import BudgetExceededError, SpendMeter, TokenUsage

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
CONTEXT_BETA = "context-management-2025-06-27"
UPDATES_BETA = "thinking-display-updates-2026-08-18"
COMPUTER_TOOLSET = "computer_toolset_20260801"
INTERRUPTED_SENTINEL = "This part of the response was interrupted before it finished."
TASK_SENTINEL = "[[TASK]]"

# Models that return progress updates with thinking.display = "updates".
UPDATES_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5")

# Long screen-driving tasks accumulate screenshots. Old tool results are cleared
# server-side (the client history stays append-only, so thinking blocks stay valid);
# clearing in large batches keeps the prompt cache effective between clears.
CONTEXT_MANAGEMENT = {
    "edits": [
        {
            "type": "clear_tool_uses_20250919",
            "trigger": {"type": "input_tokens", "value": 80_000},
            "keep": {"type": "tool_uses", "value": 8},
            "clear_at_least": {"type": "input_tokens", "value": 20_000},
            "exclude_tools": ["update_plan"],
        }
    ]
}

_STOP_MAP = {
    "end_turn": StopKind.END,
    "stop_sequence": StopKind.END,
    "tool_use": StopKind.TOOL_USE,
    "max_tokens": StopKind.MAX_TOKENS,
    "refusal": StopKind.REFUSAL,
    "pause_turn": StopKind.PAUSE,
}


@contextlib.asynccontextmanager
async def _api_errors(model: str) -> AsyncIterator[None]:
    """Translate SDK errors into LLMError with a clear message and retry hint."""
    try:
        yield
    except ValueError as exc:
        # Eager input streaming: the SDK couldn't parse a tool input at all.
        raise LLMError(f"model produced unparseable tool input: {exc}", retryable=True) from exc
    except anthropic.AuthenticationError as exc:
        raise LLMError("Anthropic API key was rejected - update it in Setup") from exc
    except anthropic.PermissionDeniedError as exc:
        raise LLMError(f"Anthropic API permission denied: {exc.message}") from exc
    except anthropic.NotFoundError as exc:
        raise LLMError(f"unknown model {model!r}: {exc.message}") from exc
    except anthropic.BadRequestError as exc:
        raise LLMError(f"Anthropic rejected the request: {exc.message}") from exc
    except anthropic.RateLimitError as exc:
        raise LLMError("Anthropic rate limit reached; try again shortly", retryable=True) from exc
    except anthropic.APIStatusError as exc:
        raise LLMError(
            f"Anthropic API error {exc.status_code}: {exc.message}",
            retryable=exc.status_code >= 500,
        ) from exc
    except anthropic.APIConnectionError as exc:
        raise LLMError("cannot reach the Anthropic API", retryable=True) from exc


def _token_usage(u: Any) -> TokenUsage:
    return TokenUsage(
        input_tokens=int(u.input_tokens or 0),
        output_tokens=int(u.output_tokens or 0),
        cache_read_tokens=int(getattr(u, "cache_read_input_tokens", 0) or 0),
        cache_write_tokens=int(getattr(u, "cache_creation_input_tokens", 0) or 0),
    )


def _check_budget(meter: SpendMeter | None) -> None:
    if meter is None:
        return
    try:
        meter.check()
    except BudgetExceededError as exc:
        raise LLMError(str(exc)) from exc


def _tool_result(r: ToolOutcome) -> dict[str, Any]:
    block: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": r.call_id,
        "is_error": r.is_error,
    }
    if r.images:
        content: list[dict[str, Any]] = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": base64.standard_b64encode(png).decode("ascii"),
                },
            }
            for png in r.images
        ]
        if r.content:
            content.append({"type": "text", "text": r.content})
        block["content"] = content
    else:
        block["content"] = r.content
    if r.toolset:
        block["toolset_name"] = r.toolset  # every toolset result must echo it
    return block


class AnthropicConversation:
    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        cfg: AnthropicConfig,
        system: str,
        tools: list[ToolSpec],
        computer_use: bool = False,
        meter: SpendMeter | None = None,
    ) -> None:
        self._client = client
        self._cfg = cfg
        self._meter = meter
        # Stable prefix (tools -> system) is cached; volatile context goes in user turns.
        self._system: list[dict[str, Any]] = [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
        ]
        self._tools: list[dict[str, Any]] = [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
                # Inputs stream as generated; the registry validates them before running.
                "eager_input_streaming": True,
            }
            for t in tools
        ]
        if computer_use:
            # All 17 members (screenshot, clicks, type, key, scroll, zoom, ...) enabled.
            self._tools.append({"type": COMPUTER_TOOLSET})
        self.messages: list[BetaMessageParam] = []

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def add_tool_results(self, results: list[ToolOutcome]) -> None:
        # All results for one assistant turn go back in a single user message.
        self.messages.append(
            {
                "role": "user",
                "content": [_tool_result(r) for r in results],  # type: ignore[misc]
            }
        )

    def _request(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self._cfg.model,
            "max_tokens": self._cfg.max_tokens,
            "system": self._system,
            "tools": self._tools,
            "messages": self.messages,
            "output_config": {"effort": self._cfg.effort.value},
            # Cache the growing conversation prefix between agent turns.
            "cache_control": {"type": "ephemeral"},
            "betas": [CONTEXT_BETA],
            "context_management": CONTEXT_MANAGEMENT,
        }
        if self._cfg.model.startswith(UPDATES_MODELS):
            kwargs["thinking"] = {"type": "adaptive", "display": "updates"}
            kwargs["betas"].append(UPDATES_BETA)
        if self._cfg.server_fallback:
            kwargs["betas"].append(FALLBACK_BETA)
            kwargs["fallbacks"] = "default"
        return kwargs

    async def step(self, on_delta: DeltaSink | None = None) -> TurnResult:
        _check_budget(self._meter)
        async with (
            _api_errors(self._cfg.model),
            self._client.beta.messages.stream(**self._request()) as stream,
        ):
            if on_delta is not None:
                await _forward(stream, on_delta)
            message: BetaMessage = await stream.get_final_message()
        if self._meter is not None:
            self._meter.record(self._cfg.model, _token_usage(message.usage))

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
            ToolCall(
                b.id,
                b.name,
                b.input if isinstance(b.input, dict) else {},
                getattr(b, "toolset_name", None),
            )
            for b in message.content
            if b.type == "tool_use"
        ]
        return TurnResult(text, calls, stop, usage)


async def _forward(stream: Any, on_delta: DeltaSink) -> None:
    """Pass text deltas through as they arrive; each non-empty thinking block (under
    display="updates" these are progress notes) as one progress event when it ends."""
    block_type: str | None = None
    progress: list[str] = []
    async for event in stream:
        etype = getattr(event, "type", None)
        if etype == "content_block_start":
            block_type = event.content_block.type
            progress = []
        elif etype == "content_block_delta":
            delta = event.delta
            if delta.type == "text_delta" and delta.text:
                await on_delta("text", delta.text)
            elif delta.type == "thinking_delta" and delta.thinking:
                progress.append(delta.thinking)
        elif etype == "content_block_stop" and block_type == "thinking":
            note = "".join(progress).strip()
            if note and note != INTERRUPTED_SENTINEL:
                await on_delta("progress", note)
            block_type = None


class QuickResponder:
    """Answers conversational turns with the fast model, without tools.

    The model is told to reply exactly ``[[TASK]]`` when the request needs any action;
    the stream is abandoned as soon as that marker appears, so routing costs a few
    output tokens and adds almost no latency.
    """

    def __init__(
        self, client: anthropic.AsyncAnthropic, model: str, meter: SpendMeter | None
    ) -> None:
        self._client = client
        self.model = model
        self._meter = meter

    async def respond(
        self,
        system: str,
        history: list[tuple[str, str]],
        text: str,
        on_delta: DeltaSink | None = None,
    ) -> str | None:
        """The reply, or None if the request should go to the full agent."""
        _check_budget(self._meter)
        messages: list[dict[str, str]] = []
        for user, assistant in history[-6:]:
            messages += [
                {"role": "user", "content": user},
                {"role": "assistant", "content": assistant},
            ]
        messages.append({"role": "user", "content": text})
        reply: list[str] = []
        held = ""
        routed = False
        async with (
            _api_errors(self.model),
            self._client.messages.stream(
                model=self.model,
                max_tokens=1024,
                system=system,
                messages=messages,  # type: ignore[arg-type]
            ) as stream,
        ):
            async for chunk in stream.text_stream:
                reply.append(chunk)
                if not routed:
                    held += chunk
                    stripped = held.lstrip()
                    if stripped.startswith(TASK_SENTINEL[: len(stripped)]) and len(stripped) < len(
                        TASK_SENTINEL
                    ):
                        continue  # could still be the marker; keep holding
                    if stripped.startswith(TASK_SENTINEL):
                        break
                    routed = True
                    if on_delta is not None:
                        await on_delta("text", held)
                elif on_delta is not None:
                    await on_delta("text", chunk)
            final = await stream.get_final_message() if routed else None
        if self._meter is not None and final is not None:
            self._meter.record(self.model, _token_usage(final.usage))
        answer = "".join(reply).strip()
        if not routed or answer.startswith(TASK_SENTINEL):
            return None
        return answer


class AnthropicProvider:
    name = "anthropic"
    supports_computer_use = True

    def __init__(
        self, cfg: AnthropicConfig, api_key: str | None, meter: SpendMeter | None = None
    ) -> None:
        self._cfg = cfg
        self._meter = meter
        self._client = anthropic.AsyncAnthropic(api_key=api_key, timeout=cfg.timeout_s)
        self.quick: QuickResponder | None = (
            QuickResponder(self._client, cfg.fast_model, meter) if cfg.fast_model else None
        )

    def new_conversation(
        self, system: str, tools: list[ToolSpec], computer_use: bool = False
    ) -> AnthropicConversation:
        return AnthropicConversation(
            self._client, self._cfg, system, tools, computer_use, self._meter
        )

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
