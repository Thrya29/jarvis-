"""Local models via Ollama's /api/chat (fully offline)."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx2 as httpx

from jarvis.core.config import OllamaConfig
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


class OllamaConversation:
    def __init__(
        self, client: httpx.AsyncClient, cfg: OllamaConfig, system: str, tools: list[ToolSpec]
    ) -> None:
        self._client = client
        self._cfg = cfg
        self._tools = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                },
            }
            for t in tools
        ]
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": system}]

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def add_tool_results(self, results: list[ToolOutcome]) -> None:
        for r in results:
            content = f"ERROR: {r.content}" if r.is_error else r.content
            if r.images:
                content += "\n[image omitted: this model cannot see screenshots]"
            self.messages.append({"role": "tool", "tool_name": r.name, "content": content})

    async def step(self, on_delta: DeltaSink | None = None) -> TurnResult:
        body = {
            "model": self._cfg.model,
            "messages": self.messages,
            "tools": self._tools,
            "stream": False,
            "options": {"num_ctx": self._cfg.num_ctx},
        }
        try:
            resp = await self._client.post("/api/chat", json=body)
        except httpx.TimeoutException as exc:
            raise LLMError("Ollama timed out", retryable=True) from exc
        except httpx.TransportError as exc:
            raise LLMError(
                f"cannot reach Ollama at {self._cfg.host} - is it running?", retryable=True
            ) from exc
        if resp.status_code == 404:
            raise LLMError(f"Ollama model {self._cfg.model!r} not found - `ollama pull` it first")
        if resp.status_code >= 400:
            raise LLMError(
                f"Ollama error {resp.status_code}: {resp.text[:300]}",
                retryable=resp.status_code >= 500,
            )
        data = resp.json()
        msg: dict[str, Any] = data.get("message") or {}
        self.messages.append(msg)

        calls: list[ToolCall] = []
        for raw in msg.get("tool_calls") or []:
            fn = raw.get("function") or {}
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"__invalid_json__": args}
            calls.append(ToolCall(f"call_{uuid.uuid4().hex[:12]}", str(fn.get("name")), args))

        if calls:
            stop = StopKind.TOOL_USE
        elif data.get("done_reason") == "length":
            stop = StopKind.MAX_TOKENS
        else:
            stop = StopKind.END
        usage = Usage(
            input_tokens=int(data.get("prompt_eval_count") or 0),
            output_tokens=int(data.get("eval_count") or 0),
        )
        return TurnResult(str(msg.get("content") or ""), calls, stop, usage)


class OllamaProvider:
    name = "ollama"
    supports_computer_use = False
    quick = None  # small local models answer everything through the agent

    def __init__(
        self, cfg: OllamaConfig, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._cfg = cfg
        self._client = httpx.AsyncClient(
            base_url=cfg.host, timeout=cfg.timeout_s, transport=transport
        )

    def new_conversation(
        self,
        system: str,
        tools: list[ToolSpec],
        computer_use: bool = False,
        web_research: bool = False,  # no server-side search for local models
    ) -> OllamaConversation:
        return OllamaConversation(self._client, self._cfg, system, tools)

    async def check(self) -> tuple[bool, str]:
        try:
            resp = await self._client.get("/api/tags", timeout=3)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            return False, f"not reachable at {self._cfg.host} ({exc})"
        names = [m.get("name", "") for m in resp.json().get("models", [])]
        if not any(n == self._cfg.model or n.split(":")[0] == self._cfg.model for n in names):
            return False, f"model {self._cfg.model} not pulled (have: {', '.join(names) or 'none'})"
        return True, f"{self._cfg.model} ready"

    async def aclose(self) -> None:
        await self._client.aclose()
