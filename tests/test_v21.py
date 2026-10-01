"""V2.1: web research with sources, and 'Ask my documents'."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from anthropic.types.beta import BetaMessage

from jarvis.core.config import AnthropicConfig, BudgetConfig, Settings
from jarvis.knowledge.index import DocIndex, chunk_text
from jarvis.llm.anthropic_provider import AnthropicProvider
from jarvis.llm.base import ToolCall
from jarvis.llm.budget import WEB_SEARCH_USD, SpendMeter, TokenUsage, estimate_usd
from jarvis.safety.policy import PathGuard
from jarvis.tools.base import ToolRegistry
from jarvis.tools.knowledge import KNOWLEDGE_TOOLS
from tests.fakes import make_ctx
from tests.test_providers import TOOLS, _FakeStream

# ======================================================================= web research


def _cited_answer() -> BetaMessage:
    citation = {
        "type": "web_search_result_location",
        "url": "https://example.org/a",
        "title": "Example A",
        "encrypted_index": "x",
        "cited_text": "fact",
    }
    return BetaMessage.model_validate(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5-5",
            "content": [
                {"type": "text", "text": "Fact one.", "citations": [citation]},
                {"type": "text", "text": " Again.", "citations": [citation]},
            ],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 100,
                "server_tool_use": {"web_search_requests": 3, "web_fetch_requests": 1},
            },
        }
    )


async def test_web_tools_sent_and_sources_returned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    meter = SpendMeter(BudgetConfig(daily_usd=100), tmp_path / "usage.db")
    provider = AnthropicProvider(AnthropicConfig(), api_key="sk-ant-test", meter=meter)
    sent: list[dict[str, Any]] = []

    def fake_stream(**kwargs: Any) -> _FakeStream:
        sent.append(kwargs)
        return _FakeStream(_cited_answer())

    monkeypatch.setattr(provider._client.beta.messages, "stream", fake_stream)

    off = provider.new_conversation("S", TOOLS)
    off.add_user("hi")
    await off.step()
    assert not any(str(t.get("type", "")).startswith("web_") for t in sent[0]["tools"])

    conv = provider.new_conversation("S", TOOLS, web_research=True)
    conv.add_user("what's new?")
    turn = await conv.step()
    types = {t.get("type") for t in sent[1]["tools"]}
    assert {"web_search_20260209", "web_fetch_20260209"} <= types
    # Server tools must not carry client-tool-only fields.
    for t in sent[1]["tools"]:
        if str(t.get("type", "")).startswith("web_"):
            assert "eager_input_streaming" not in t
    assert [(s.url, s.title) for s in turn.sources] == [("https://example.org/a", "Example A")]
    # Search fees count toward the daily budget.
    assert meter.today_usd() >= 2 * 3 * WEB_SEARCH_USD


def test_search_fee_in_estimate() -> None:
    base = estimate_usd("claude-opus-5-5", TokenUsage(input_tokens=0, output_tokens=0))
    with_search = estimate_usd(
        "claude-opus-5-5", TokenUsage(input_tokens=0, output_tokens=0, web_searches=5)
    )
    assert with_search - base == pytest.approx(5 * WEB_SEARCH_USD)


# ======================================================================= documents


class FakeEmbedder:
    """Deterministic bag-of-words vectors: enough to test ranking without a model."""

    DIM = 256

    def embed(self, texts: list[str], batch: int = 16) -> np.ndarray:
        out = np.zeros((len(texts), self.DIM), dtype=np.float32)
        for i, text in enumerate(texts):
            for word in text.lower().split():
                word = word.strip(".,?!:;()\"'")
                if len(word) > 2:
                    h = int(hashlib.sha256(word.encode()).hexdigest(), 16)
                    out[i, h % self.DIM] += 1.0
            n = np.linalg.norm(out[i])
            if n:
                out[i] /= n
        return out


def test_chunk_text_overlaps_and_covers() -> None:
    text = " ".join(f"word{i}" for i in range(1000))
    chunks = chunk_text(text, size=300, overlap=50)
    assert len(chunks) > 1 and all(len(c) <= 300 for c in chunks)
    assert chunks[0].split()[0] == "word0" and "word999" in chunks[-1]


def test_index_sync_search_and_guard(tmp_path: Path) -> None:
    root = tmp_path / "docs"
    (root / "sub").mkdir(parents=True)
    (root / "contract.md").write_text(
        "Acme contract. Payment terms are net 45 days.", encoding="utf-8"
    )
    (root / "sub" / "recipe.txt").write_text("Pancakes need flour, eggs and milk.", "utf-8")
    outside = tmp_path / "secret"
    outside.mkdir()
    (outside / "keys.txt").write_text("payment password hunter2", encoding="utf-8")

    index = DocIndex(tmp_path / "documents.db", FakeEmbedder())  # type: ignore[arg-type]
    guard = PathGuard([root])
    report = index.sync([root, outside], guard)
    assert report.indexed == 2 and [Path(f).name for f in report.skipped_folders] == ["secret"]

    hits = index.search("payment terms acme", 3)
    assert Path(hits[0].path).name == "contract.md"
    assert all("hunter2" not in h.text for h in hits)

    again = index.sync([root], guard)
    assert again.indexed == 0 and again.unchanged == 2  # incremental
    (root / "sub" / "recipe.txt").unlink()
    assert index.sync([root], guard).removed == 1
    assert index.stats()["files"] == 1
    index.close()


async def test_search_documents_tool(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "notes.md").write_text("The firmware release moved to Tuesday.", encoding="utf-8")
    settings = Settings(  # type: ignore[arg-type]
        safety={"allowed_roots": [root]}, features={"documents": {"enabled": True}}
    )
    ctx = make_ctx(root, settings=settings)
    reg = ToolRegistry(KNOWLEDGE_TOOLS)

    off = await reg.execute(ToolCall("1", "search_documents", {"query": "firmware"}), ctx)
    assert off.is_error and "turned off" in off.content

    ctx.documents = DocIndex(tmp_path / "d.db", FakeEmbedder())  # type: ignore[arg-type]
    out = await reg.execute(ToolCall("2", "search_documents", {"query": "firmware release"}), ctx)
    assert not out.is_error and "notes.md" in out.content
    assert "<untrusted_content" in out.content  # file text is data, not instructions
    ctx.documents.close()
