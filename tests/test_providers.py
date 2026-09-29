from __future__ import annotations

import json
from typing import Any

import httpx2 as httpx
import pytest
from anthropic.types.beta import BetaMessage

from jarvis.core.config import AnthropicConfig, OllamaConfig
from jarvis.llm.anthropic_provider import FALLBACK_BETA, AnthropicProvider
from jarvis.llm.base import LLMError, StopKind, ToolOutcome, ToolSpec
from jarvis.llm.ollama_provider import OllamaProvider

TOOLS = [ToolSpec("write_file", "Write", {"type": "object", "properties": {}})]


def _message(content: list[dict[str, Any]], stop: str) -> BetaMessage:
    return BetaMessage.model_validate(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5-5",
            "content": content,
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 80},
        }
    )


class _FakeStream:
    def __init__(self, msg: BetaMessage | Exception) -> None:
        self._msg = msg

    async def __aenter__(self) -> _FakeStream:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get_final_message(self) -> BetaMessage:
        if isinstance(self._msg, Exception):
            raise self._msg
        return self._msg


def _anthropic(monkeypatch: pytest.MonkeyPatch, replies: list[Any]) -> tuple[Any, list[dict]]:
    provider = AnthropicProvider(AnthropicConfig(), api_key="sk-ant-test")
    sent: list[dict[str, Any]] = []

    def fake_stream(**kwargs: Any) -> _FakeStream:
        sent.append({**kwargs, "messages": list(kwargs["messages"])})
        return _FakeStream(replies.pop(0))

    monkeypatch.setattr(provider._client.beta.messages, "stream", fake_stream)
    return provider, sent


async def test_anthropic_request_shape_and_tool_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    tool_msg = _message(
        [
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "text", "text": "Writing."},
            {"type": "tool_use", "id": "toolu_1", "name": "write_file", "input": {"path": "a"}},
        ],
        "tool_use",
    )
    provider, sent = _anthropic(
        monkeypatch, [tool_msg, _message([{"type": "text", "text": "Done"}], "end_turn")]
    )
    conv = provider.new_conversation("SYSTEM", TOOLS)
    conv.add_user("goal")
    turn = await conv.step()

    req = sent[0]
    assert req["model"] == "claude-opus-5-5"
    assert req["output_config"] == {"effort": "high"}
    assert req["fallbacks"] == "default" and req["betas"] == [FALLBACK_BETA]
    assert "thinking" not in req and "tool_choice" not in req
    assert req["tools"][0]["eager_input_streaming"] is True
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}

    assert turn.stop is StopKind.TOOL_USE and turn.text == "Writing."
    assert turn.tool_calls[0].input == {"path": "a"}
    assert turn.usage.cache_read_tokens == 80

    conv.add_tool_results([ToolOutcome("toolu_1", "write_file", "ok")])
    turn2 = await conv.step()
    assert turn2.stop is StopKind.END and turn2.text == "Done"
    history = sent[1]["messages"]
    # Assistant content (incl. the thinking block) is replayed unchanged.
    assert history[1]["content"] is tool_msg.content
    assert history[2]["content"][0]["tool_use_id"] == "toolu_1"


async def test_anthropic_refusal_not_added_to_history(monkeypatch: pytest.MonkeyPatch) -> None:
    provider, _ = _anthropic(monkeypatch, [_message([], "refusal")])
    conv = provider.new_conversation("S", TOOLS)
    conv.add_user("x")
    turn = await conv.step()
    assert turn.stop is StopKind.REFUSAL
    assert len(conv.messages) == 1


async def test_anthropic_unparseable_tool_input_is_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, _ = _anthropic(monkeypatch, [ValueError("bad json")])
    conv = provider.new_conversation("S", TOOLS)
    conv.add_user("x")
    with pytest.raises(LLMError) as exc:
        await conv.step()
    assert exc.value.retryable and len(conv.messages) == 1


async def test_fallback_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = AnthropicProvider(AnthropicConfig(server_fallback=False), api_key="k")
    sent: list[dict[str, Any]] = []

    def fake_stream(**kwargs: Any) -> _FakeStream:
        sent.append(kwargs)
        return _FakeStream(_message([{"type": "text", "text": "hi"}], "end_turn"))

    monkeypatch.setattr(provider._client.beta.messages, "stream", fake_stream)
    conv = provider.new_conversation("S", TOOLS)
    conv.add_user("x")
    await conv.step()
    assert "fallbacks" not in sent[0] and "betas" not in sent[0]


def _ollama(handler: Any) -> OllamaProvider:
    return OllamaProvider(OllamaConfig(), transport=httpx.MockTransport(handler))


async def test_ollama_tool_call_roundtrip() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            msg = {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "write_file", "arguments": {"path": "a"}}}],
            }
        else:
            msg = {"role": "assistant", "content": "All done"}
        return httpx.Response(200, json={"message": msg, "done_reason": "stop", "eval_count": 7})

    provider = _ollama(handler)
    conv = provider.new_conversation("SYS", TOOLS)
    conv.add_user("goal")
    turn = await conv.step()
    assert turn.stop is StopKind.TOOL_USE
    assert turn.tool_calls[0].name == "write_file" and turn.tool_calls[0].input == {"path": "a"}
    assert bodies[0]["tools"][0]["function"]["name"] == "write_file"
    assert bodies[0]["messages"][0] == {"role": "system", "content": "SYS"}

    conv.add_tool_results([ToolOutcome(turn.tool_calls[0].id, "write_file", "boom", is_error=True)])
    turn = await conv.step()
    assert turn.stop is StopKind.END and turn.text == "All done"
    assert bodies[1]["messages"][-1] == {
        "role": "tool",
        "tool_name": "write_file",
        "content": "ERROR: boom",
    }
    await provider.aclose()


async def test_ollama_errors() -> None:
    provider = _ollama(lambda r: httpx.Response(404, json={"error": "model not found"}))
    conv = provider.new_conversation("S", TOOLS)
    with pytest.raises(LLMError, match="ollama pull"):
        await conv.step()

    def boom(r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    conv = _ollama(boom).new_conversation("S", TOOLS)
    with pytest.raises(LLMError) as exc:
        await conv.step()
    assert exc.value.retryable


async def test_ollama_check() -> None:
    provider = _ollama(lambda r: httpx.Response(200, json={"models": [{"name": "qwen2.5:3b"}]}))
    assert (await provider.check())[0]
    provider = _ollama(lambda r: httpx.Response(200, json={"models": []}))
    ok, detail = await provider.check()
    assert not ok and "not pulled" in detail
