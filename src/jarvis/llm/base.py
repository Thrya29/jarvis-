"""Provider-neutral LLM interface.

Each provider owns its conversation history in its native wire format (so, e.g.,
Claude's thinking blocks are replayed byte-for-byte) and exposes a small neutral
surface the agent loop drives.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol


class StopKind(StrEnum):
    END = "end"  # model finished its turn
    TOOL_USE = "tool_use"  # model wants tools run
    MAX_TOKENS = "max_tokens"  # output cut off
    REFUSAL = "refusal"  # declined by safety classifier
    PAUSE = "pause"  # provider asked us to resume


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]
    toolset: str | None = None  # e.g. "computer" for Claude's computer-use toolset


@dataclass(frozen=True)
class ToolOutcome:
    call_id: str
    name: str
    content: str
    is_error: bool = False
    images: tuple[bytes, ...] = ()  # PNG screenshots
    toolset: str | None = None


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens


@dataclass(frozen=True)
class TurnResult:
    text: str
    tool_calls: list[ToolCall]
    stop: StopKind
    usage: Usage = field(default_factory=Usage)
    detail: str | None = None  # e.g. refusal category


class LLMError(RuntimeError):
    """A provider call failed after the provider's own retries."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class Conversation(Protocol):
    def add_user(self, text: str) -> None: ...

    def add_tool_results(self, results: list[ToolOutcome]) -> None: ...

    async def step(self) -> TurnResult:
        """Send the history to the model and append its reply to the history."""
        ...


class LLMProvider(Protocol):
    name: str
    # Whether the model can drive the desktop from screenshots (Claude's computer toolset).
    supports_computer_use: bool

    def new_conversation(
        self, system: str, tools: list[ToolSpec], computer_use: bool = False
    ) -> Conversation: ...

    async def check(self) -> tuple[bool, str]:
        """Cheap reachability/credential check for `jarvis doctor`."""
        ...

    async def aclose(self) -> None: ...
