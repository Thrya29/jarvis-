"""V2.0: settings/onboarding, persona, streaming, quick replies, budget, voice choice."""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from jarvis.agent.factory import all_tools
from jarvis.agent.loop import Agent, TaskStatus
from jarvis.agent.prompts import build_system_prompt, goal_message, persona_text, quick_prompt
from jarvis.core.config import (
    AddressMode,
    AgentConfig,
    BudgetConfig,
    PersonaConfig,
    PersonaStyle,
    ProfileConfig,
    ProtocolsFeature,
    Settings,
    VoiceConfig,
    load_settings,
)
from jarvis.core.paths import AppPaths
from jarvis.llm.anthropic_provider import TASK_SENTINEL, QuickResponder, _forward
from jarvis.llm.base import LLMError, StopKind, TurnResult, Usage
from jarvis.llm.budget import SpendMeter, TokenUsage, estimate_usd
from jarvis.server.app import create_app
from jarvis.server.hub import Hub
from jarvis.tools.base import ToolRegistry
from jarvis.voice.engine import VoiceAssistant, split_complete
from jarvis.voice.models import FILES, piper_files
from tests.fakes import EventLog, ScriptedProvider, make_ctx, text

TOKEN = "t" * 43
AUTH = {"Authorization": f"Bearer {TOKEN}"}


# ======================================================================= settings model


@pytest.mark.parametrize(
    ("address", "name", "nick", "expected"),
    [
        (AddressMode.SIR, "", "", "sir"),
        (AddressMode.MAAM, "", "", "ma'am"),
        (AddressMode.NAME, "Priya Sharma", "", "Priya"),
        (AddressMode.NICKNAME, "", "Boss", "Boss"),
        (AddressMode.NAME, "", "", None),
        (AddressMode.NONE, "Priya", "", None),
    ],
)
def test_addressee(address: AddressMode, name: str, nick: str, expected: str | None) -> None:
    assert ProfileConfig(address=address, name=name, nickname=nick).addressee() == expected


def test_voice_choices_and_legacy_names() -> None:
    assert VoiceConfig(tts_voice="lessac").tts_voice == "american_female"
    assert VoiceConfig(tts_voice="af_heart").tts_voice == "american_female"
    for v in ("british_male", "british_female", "american_male", "american_female"):
        assert VoiceConfig(tts_voice=v).tts_voice == v
        assert all(f in FILES for f in piper_files(v))  # pinned + hashed
    with pytest.raises(ValidationError):
        VoiceConfig(tts_voice="morgan_freeman")


def test_briefing_time_validated() -> None:
    assert ProtocolsFeature(briefing_time="07:05").briefing_time == "07:05"
    for bad in ("7:05", "25:00", "08:61", "noon"):
        with pytest.raises(ValidationError):
            ProtocolsFeature(briefing_time=bad)


# ======================================================================= budget


def test_estimate_usd() -> None:
    u = TokenUsage(input_tokens=1_000_000, output_tokens=100_000, cache_read_tokens=1_000_000)
    assert estimate_usd("claude-opus-5-5", u) == pytest.approx(4.0 + 2.0 + 0.2)
    assert estimate_usd("claude-haiku-4-5", TokenUsage(output_tokens=1_000_000)) == pytest.approx(5)
    # Unknown models are priced conservatively (most expensive tier).
    assert estimate_usd("mystery", TokenUsage(output_tokens=1_000_000)) == pytest.approx(50)


def test_meter_cap(tmp_path: Path) -> None:
    cfg = BudgetConfig(daily_usd=1.0)
    meter = SpendMeter(cfg, tmp_path / "usage.db")
    meter.check()
    meter.record("claude-opus-5-5", TokenUsage(output_tokens=40_000))  # $0.80
    assert meter.today_usd() == pytest.approx(0.8)
    assert meter.remaining_usd() == pytest.approx(0.2)
    meter.record("claude-opus-5-5", TokenUsage(output_tokens=20_000))
    with pytest.raises(RuntimeError, match="budget"):
        meter.check()
    assert meter.today_usd(date(2000, 1, 1)) == 0
    cfg.daily_usd = 0  # 0 = no limit; settings changes apply in place
    meter.check()
    assert meter.remaining_usd() is None


# ======================================================================= prompts


def test_persona_prompt() -> None:
    profile = ProfileConfig(name="Priya Sharma", address=AddressMode.SIR)
    persona = PersonaConfig(style=PersonaStyle.MINIMAL, pushback=False)
    t = persona_text(profile, persona)
    assert "Priya Sharma" in t and '"sir"' in t and "as brief as possible" in t
    assert "unwise" not in t
    none = persona_text(ProfileConfig(), PersonaConfig())
    assert "Don't use titles" in none and "unwise" in none
    sp = build_system_prompt([Path.home()], profile=profile, persona=persona)
    assert "# Personality" in sp
    assert "# Personality" not in build_system_prompt([Path.home()])


def test_quick_prompt_and_recent_chat() -> None:
    q = quick_prompt(ProfileConfig(), PersonaConfig(), voice=True)
    assert TASK_SENTINEL in q and "spoken aloud" in q
    g = goal_message("do it", recent_chat=[("hi", "Hello."), ("joke?", "No.")])
    assert "<recent_chat>" in g and "User: joke?" in g


# ======================================================================= streaming + quick replies


class _Ev(SimpleNamespace):
    pass


def _events(*items: Any) -> Any:
    async def gen() -> Any:
        for i in items:
            yield i

    return gen()


async def test_forward_streams_text_and_progress_notes() -> None:
    seen: list[tuple[str, str]] = []

    async def sink(kind: str, t: str) -> None:
        seen.append((kind, t))

    stream = _events(
        _Ev(type="content_block_start", content_block=_Ev(type="thinking")),
        _Ev(
            type="content_block_delta",
            delta=_Ev(type="thinking_delta", thinking="Found 12 files; "),
        ),
        _Ev(type="content_block_delta", delta=_Ev(type="thinking_delta", thinking="moving them.")),
        _Ev(type="content_block_stop"),
        _Ev(
            type="content_block_start", content_block=_Ev(type="thinking")
        ),  # empty: hidden reasoning
        _Ev(type="content_block_stop"),
        _Ev(type="content_block_start", content_block=_Ev(type="text")),
        _Ev(type="content_block_delta", delta=_Ev(type="text_delta", text="Done")),
        _Ev(type="content_block_delta", delta=_Ev(type="text_delta", text=".")),
        _Ev(type="content_block_stop"),
    )
    await _forward(stream, sink)
    assert seen == [("progress", "Found 12 files; moving them."), ("text", "Done"), ("text", ".")]


class _QuickStream:
    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks
        self.text_stream = self._gen()

    async def _gen(self) -> Any:
        for c in self._chunks:
            yield c

    async def __aenter__(self) -> _QuickStream:
        return self

    async def __aexit__(self, *a: object) -> None:
        return None

    async def get_final_message(self) -> Any:
        return SimpleNamespace(usage=SimpleNamespace(input_tokens=100, output_tokens=20))


def _quick(chunks: list[str], meter: SpendMeter | None = None) -> QuickResponder:
    client = SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: _QuickStream(chunks)))
    return QuickResponder(client, "claude-haiku-4-5", meter)  # type: ignore[arg-type]


async def test_quick_answer_streams() -> None:
    deltas: list[str] = []

    async def sink(kind: str, t: str) -> None:
        deltas.append(t)

    answer = await _quick(["It's ", "about 8 minutes."]).respond(
        "sys", [], "how far is the sun", sink
    )
    assert answer == "It's about 8 minutes." and "".join(deltas) == "It's about 8 minutes."


async def test_quick_routes_tasks_without_emitting() -> None:
    deltas: list[str] = []

    async def sink(kind: str, t: str) -> None:
        deltas.append(t)

    assert await _quick(["[[TA", "SK]]"]).respond("s", [], "open excel", sink) is None
    assert deltas == []


async def test_quick_respects_budget(tmp_path: Path) -> None:
    meter = SpendMeter(BudgetConfig(daily_usd=0.01), tmp_path / "u.db")
    meter.record("claude-opus-5-5", TokenUsage(output_tokens=10_000))
    with pytest.raises(LLMError, match="budget"):
        await _quick(["hi"], meter).respond("s", [], "hello")


# ======================================================================= agent integration


class FakeQuick:
    def __init__(self, answers: list[str | None]) -> None:
        self.answers = answers
        self.seen: list[tuple[list[tuple[str, str]], str]] = []

    async def respond(
        self, system: str, history: list[tuple[str, str]], t: str, on_delta: Any = None
    ) -> str | None:
        self.seen.append((list(history), t))
        answer = self.answers.pop(0)
        if answer and on_delta:
            await on_delta("text", answer)
        return answer


def _agent(
    tmp_path: Path, script: list[Any], quick: FakeQuick | None, events: EventLog
) -> tuple[Agent, ScriptedProvider]:
    provider = ScriptedProvider(script)
    provider.quick = quick
    agent = Agent(
        provider, ToolRegistry(all_tools()), make_ctx(tmp_path / "docs"), AgentConfig(), events
    )
    return agent, provider


async def test_quick_reply_then_task_with_chat_context(tmp_path: Path) -> None:
    events = EventLog()
    quick = FakeQuick(["Good morning.", None])
    agent, provider = _agent(tmp_path, [text("Opened it.")], quick, events)
    r1 = await agent.run("good morning")
    assert r1.status is TaskStatus.COMPLETED and r1.summary == "Good morning."
    assert provider.conversation.history == []  # the main model was never called
    assert [e["type"] for e in events.events][:3] == [
        "task.started",
        "assistant.delta",
        "assistant.text",
    ]
    r2 = await agent.run("open notepad")
    assert r2.summary == "Opened it."
    first_user = provider.conversation.history[0][1]
    assert "<recent_chat>" in first_user and "good morning" in first_user
    assert quick.seen[1][0] == [("good morning", "Good morning.")]


async def test_quick_disabled_by_persona(tmp_path: Path) -> None:
    events = EventLog()
    quick = FakeQuick(["nope"])
    agent, _ = _agent(tmp_path, [text("Full agent.")], quick, events)
    agent._ctx.settings.persona.quick_replies = False
    assert (await agent.run("hi")).summary == "Full agent."
    assert quick.seen == []


async def test_streamed_turn_and_discard_on_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = EventLog()
    agent, provider = _agent(tmp_path, [], None, events)
    calls = {"n": 0}

    async def step(on_delta: Any = None) -> TurnResult:
        calls["n"] += 1
        await on_delta("text", "Partial ")
        if calls["n"] == 1:
            raise LLMError("blip", retryable=True)
        await on_delta("progress", "Checking the folder.")
        await on_delta("text", "answer.")
        return TurnResult("Partial answer.", [], StopKind.END, Usage())

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(provider.conversation, "step", step)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    await agent.run("x")
    types = [e["type"] for e in events.events]
    assert "assistant.discard" in types and "assistant.progress" in types
    final = next(e for e in events.events if e["type"] == "assistant.text")
    assert final["streamed"] is True and final["text"] == "Partial answer."


# ======================================================================= voice


def test_split_complete() -> None:
    assert split_complete("Hi there. I moved 3 files! Now wri") == (
        "Hi there. I moved 3 files!",
        "Now wri",
    )
    assert split_complete("Version 2.0 shipped") == ("", "Version 2.0 shipped")


class _Player:
    def __init__(self) -> None:
        self.queued: list[Any] = []

    def enqueue(self, a: Any) -> None:
        self.queued.append(a)

    def pause(self) -> None: ...
    def resume(self) -> None: ...
    def clear(self) -> None:
        self.queued.clear()

    @property
    def active(self) -> bool:
        return False

    @property
    def playing(self) -> bool:
        return False


class _TTS:
    def __init__(self) -> None:
        self.spoken: list[str] = []

    async def synthesize(self, t: str) -> np.ndarray:
        self.spoken.append(t)
        return np.ones(4, dtype=np.float32)


async def _va(**kw: Any) -> tuple[VoiceAssistant, _TTS, asyncio.Task[None]]:
    tts = _TTS()
    va = VoiceAssistant(VoiceConfig(), None, tts, _Player(), log_line=lambda s: None, **kw)  # type: ignore[arg-type]
    runner = asyncio.create_task(va.run())
    await asyncio.sleep(0)
    return va, tts, runner


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


async def test_voice_speaks_while_streaming() -> None:
    va, tts, runner = await _va()
    await va.on_agent_event({"type": "assistant.delta", "text": "I found three "})
    await _settle()
    assert tts.spoken == []  # no complete sentence yet
    await va.on_agent_event({"type": "assistant.delta", "text": "files. Moving them"})
    await _settle()
    assert tts.spoken == ["I found three files."]
    await va.on_agent_event({"type": "assistant.text", "text": "…", "streamed": True})
    await _settle()
    assert tts.spoken == ["I found three files.", "Moving them."]
    runner.cancel()


async def test_progress_spoken_only_when_enabled() -> None:
    va, tts, runner = await _va(speak_progress=False)
    await va.on_agent_event({"type": "assistant.progress", "text": "Halfway there."})
    await _settle()
    assert tts.spoken == []
    runner.cancel()
    va, tts, runner = await _va(speak_progress=True)
    await va.on_agent_event({"type": "assistant.progress", "text": "Halfway there."})
    await _settle()
    assert tts.spoken == ["Halfway there."]
    runner.cancel()


async def test_spoken_approval_uses_addressee() -> None:
    from jarvis.safety.policy import ApprovalRequest, Risk

    va, tts, runner = await _va(addressee="sir")
    task = asyncio.create_task(
        va.approver.request(ApprovalRequest("x", "Delete old.txt", Risk.DESTRUCTIVE))
    )
    await _settle()
    assert tts.spoken[-1].endswith("Should I go ahead, sir?")
    await va.handle_text("yes")
    assert (await task).approved
    runner.cancel()


# ======================================================================= settings API


@pytest.fixture
def hub(paths: AppPaths) -> Hub:
    return Hub(load_settings(), paths)


@pytest.fixture
def client(hub: Hub) -> TestClient:
    return TestClient(create_app(hub.settings, TOKEN, hub=hub), base_url="http://127.0.0.1")


def test_settings_roundtrip(client: TestClient, hub: Hub) -> None:
    view = client.get("/v1/settings", headers=AUTH).json()
    assert view["profile"]["onboarded"] is False and "british_male" in view["voices"]
    gen = hub.generation
    r = client.put(
        "/v1/settings",
        headers=AUTH,
        json={
            "profile": {"name": "Alex", "address": "name", "onboarded": True},
            "persona": {"style": "friendly"},
            "voice": {"tts_voice": "british_female", "tts_speed": 1.1},
            "features": {"email": {"enabled": True, "provider": "google"}},
            "budget": {"daily_usd": 2.5},
        },
    )
    assert r.status_code == 200
    assert hub.generation == gen + 1
    s = load_settings()  # persisted
    assert s.profile.addressee() == "Alex" and s.persona.style is PersonaStyle.FRIENDLY
    assert s.voice.tts_voice == "british_female" and s.voice.tts_speed == pytest.approx(1.1)
    assert s.features.email.enabled and s.features.email.provider == "google"
    assert s.features.email.account == "personal"  # untouched nested value kept
    assert hub.settings.budget.daily_usd == 2.5  # updated in place
    status = client.get("/v1/status", headers=AUTH).json()["setup"]
    assert status["onboarded"] is True and status["daily_cap_usd"] == 2.5
    assert status["spend_today_usd"] == 0


@pytest.mark.parametrize(
    "bad",
    [
        {"profile": {"address": "your majesty"}},
        {"voice": {"stt_model": "huge"}},
        {"secrets": {"x": 1}},
        {"budget": {"daily_usd": -1}},
        {"features": {"protocols": {"briefing_time": "9am"}}},
    ],
)
def test_settings_rejects_invalid(client: TestClient, bad: dict[str, Any]) -> None:
    before = load_settings().model_dump()
    assert client.put("/v1/settings", headers=AUTH, json=bad).status_code == 422
    assert load_settings().model_dump() == before  # nothing written


def test_voice_preview(client: TestClient, monkeypatch: pytest.MonkeyPatch, hub: Hub) -> None:
    import jarvis.server.hub as hub_mod
    import jarvis.voice.speech as speech

    played: list[int] = []
    monkeypatch.setattr(hub_mod, "_play", lambda audio, device: played.append(len(audio)))

    class FakeTTS:
        def __init__(self, path: Path, speed: float) -> None:
            self.path = path

        async def synthesize(self, t: str) -> np.ndarray:
            assert "This is how I will sound" in t
            return np.ones(100, dtype=np.float32)

        def close(self) -> None: ...

    monkeypatch.setattr(speech, "PiperTTS", FakeTTS)
    from jarvis.voice.models import ModelStore

    monkeypatch.setattr(ModelStore, "missing", lambda self, names: [])
    r = client.post("/v1/voice/preview", headers=AUTH, json={"voice": "british_male", "speed": 1.0})
    assert r.status_code == 200 and played == [100]
    r = client.post("/v1/voice/preview", headers=AUTH, json={"voice": "nobody", "speed": 1.0})
    assert r.status_code == 422


def test_settings_default_budget() -> None:
    assert (
        Settings().budget.daily_usd == 5.0
        and Settings().llm.anthropic.fast_model == "claude-haiku-4-5"
    )
