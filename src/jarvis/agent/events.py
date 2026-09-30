"""Events the agent emits while working. Clients (CLI, UI, voice) render these."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

Event = dict[str, Any]
EventSink = Callable[[Event], Awaitable[None]]


async def null_sink(event: Event) -> None:
    return None
