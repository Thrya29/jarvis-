"""Estimated API spend per day, with a hard daily cap.

Costs are estimated from token usage and list prices (USD per million tokens). The
figures are for guidance and enforcement of the user's cap; the Anthropic console is
the source of truth for billing.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from jarvis.core.config import BudgetConfig

# model -> (input, output, cache read) USD per million tokens
PRICES: dict[str, tuple[float, float, float]] = {
    "claude-fable-5-1": (10.0, 50.0, 0.25),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20),
    "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-haiku-4-5": (1.0, 5.0, 0.10),
}
CACHE_WRITE_MULTIPLIER = 1.25  # 5-minute cache writes cost 1.25x base input
WEB_SEARCH_USD = 10.0 / 1000  # server-side web search: $10 per 1,000 searches


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    web_searches: int = 0


class BudgetExceededError(RuntimeError):
    pass


def estimate_usd(model: str, u: TokenUsage) -> float:
    # Unknown models are priced like the most expensive listed tier (conservative).
    price_in, price_out, price_cache = PRICES.get(model, max(PRICES.values()))
    return (
        u.input_tokens * price_in
        + u.cache_write_tokens * price_in * CACHE_WRITE_MULTIPLIER
        + u.cache_read_tokens * price_cache
        + u.output_tokens * price_out
    ) / 1_000_000 + u.web_searches * WEB_SEARCH_USD


class SpendMeter:
    def __init__(self, cfg: BudgetConfig, path: Path) -> None:
        self.cfg = cfg
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS spend (day TEXT NOT NULL, model TEXT NOT NULL, "
            "input INTEGER, output INTEGER, cache_read INTEGER, cache_write INTEGER, "
            "usd REAL NOT NULL)"
        )
        self._db.execute("CREATE INDEX IF NOT EXISTS spend_day ON spend(day)")

    def today_usd(self, day: date | None = None) -> float:
        with self._lock:
            row = self._db.execute(
                "SELECT COALESCE(SUM(usd), 0) FROM spend WHERE day = ?",
                ((day or date.today()).isoformat(),),
            ).fetchone()
        return float(row[0])

    def remaining_usd(self) -> float | None:
        if not self.cfg.daily_usd:
            return None
        return max(0.0, self.cfg.daily_usd - self.today_usd())

    def check(self) -> None:
        cap = self.cfg.daily_usd
        if cap and self.today_usd() >= cap:
            raise BudgetExceededError(
                f"today's API budget of ${cap:.2f} is used up. Raise it in Settings → Budget, "
                "or it resets at midnight."
            )

    def record(self, model: str, usage: TokenUsage) -> float:
        usd = estimate_usd(model, usage)
        with self._lock:
            self._db.execute(
                "INSERT INTO spend VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    date.today().isoformat(),
                    model,
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.cache_read_tokens,
                    usage.cache_write_tokens,
                    usd,
                ),
            )
        return usd

    def close(self) -> None:
        with self._lock:
            self._db.close()
