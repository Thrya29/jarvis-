"""Risk tiers, path confinement, and the human-approval contract."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Protocol

from jarvis.core.config import SafetyConfig


class Risk(IntEnum):
    READ = 0  # observes only
    WRITE = 1  # creates new things inside allowed folders
    EXECUTE = 2  # runs arbitrary code / commands
    DESTRUCTIVE = 3  # deletes or overwrites existing data
    EXTERNAL = 4  # effects outside this machine (email, uploads, purchases)


def needs_approval(risk: Risk, cfg: SafetyConfig) -> bool:
    if risk is Risk.READ:
        return False
    if risk is Risk.WRITE:
        return not cfg.auto_approve_writes
    if risk is Risk.EXECUTE:
        return cfg.confirm_risky_actions
    return True  # DESTRUCTIVE and EXTERNAL always ask, whatever the config says


class PathDeniedError(PermissionError):
    pass


class PathGuard:
    """Confines file access to the configured roots, after resolving links/junctions."""

    def __init__(self, allowed_roots: list[Path], deny_roots: list[Path] | None = None) -> None:
        self.allowed = [self._real(r) for r in allowed_roots]
        self.denied = [self._real(r) for r in (deny_roots or [])]
        if not self.allowed:
            raise ValueError("at least one allowed root is required")

    @staticmethod
    def _real(p: Path) -> Path:
        return Path(os.path.realpath(p.expanduser()))

    @property
    def default_root(self) -> Path:
        return self.allowed[0]

    def resolve(self, raw: str | Path) -> Path:
        p = Path(os.path.expandvars(str(raw))).expanduser()
        if not p.is_absolute():
            p = self.default_root / p
        real = self._real(p)
        if any(real == d or real.is_relative_to(d) for d in self.denied):
            raise PathDeniedError(f"{real} is a protected JARVIS location")
        if not any(real == a or real.is_relative_to(a) for a in self.allowed):
            roots = ", ".join(str(a) for a in self.allowed)
            raise PathDeniedError(f"{real} is outside the allowed folders ({roots})")
        return real


@dataclass(frozen=True)
class ApprovalRequest:
    tool: str
    summary: str
    risk: Risk
    details: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass(frozen=True)
class ApprovalDecision:
    approved: bool
    note: str = ""


class Approver(Protocol):
    async def request(self, req: ApprovalRequest) -> ApprovalDecision: ...

    async def ask(self, question: str) -> str:
        """Ask the user a free-form clarifying question; empty string if unavailable."""
        ...


class DenyAllApprover:
    """Headless default: nothing that needs a human is allowed to happen."""

    async def request(self, req: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(False, "no user available to approve")

    async def ask(self, question: str) -> str:
        return ""
