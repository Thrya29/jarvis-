from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.core.config import SafetyConfig
from jarvis.safety.policy import PathDeniedError, PathGuard, Risk, needs_approval


@pytest.fixture
def guard(tmp_path: Path) -> PathGuard:
    (tmp_path / "allowed" / "sub").mkdir(parents=True)
    (tmp_path / "allowed" / "secret").mkdir()
    return PathGuard([tmp_path / "allowed"], deny_roots=[tmp_path / "allowed" / "secret"])


def test_allows_inside_root(guard: PathGuard, tmp_path: Path) -> None:
    assert guard.resolve(tmp_path / "allowed" / "sub" / "x.txt").name == "x.txt"


def test_relative_paths_resolve_against_first_root(guard: PathGuard, tmp_path: Path) -> None:
    assert guard.resolve("sub/x.txt") == (tmp_path / "allowed" / "sub" / "x.txt").resolve()


@pytest.mark.parametrize("bad", ["../outside.txt", "sub/../../outside.txt", "C:/Windows/win.ini"])
def test_rejects_escape(guard: PathGuard, bad: str) -> None:
    with pytest.raises(PathDeniedError):
        guard.resolve(bad)


def test_rejects_sibling_with_common_prefix(guard: PathGuard, tmp_path: Path) -> None:
    (tmp_path / "allowed-evil").mkdir()
    with pytest.raises(PathDeniedError):
        guard.resolve(tmp_path / "allowed-evil" / "x")


def test_deny_roots_win(guard: PathGuard) -> None:
    with pytest.raises(PathDeniedError, match="protected"):
        guard.resolve("secret/token")


def test_approval_matrix() -> None:
    cfg = SafetyConfig(allowed_roots=[Path.cwd()])
    assert not needs_approval(Risk.READ, cfg)
    assert not needs_approval(Risk.WRITE, cfg)
    assert needs_approval(Risk.EXECUTE, cfg)
    assert needs_approval(Risk.DESTRUCTIVE, cfg)
    assert needs_approval(Risk.EXTERNAL, cfg)


def test_destructive_always_confirms_even_if_opted_out() -> None:
    cfg = SafetyConfig(
        allowed_roots=[Path.cwd()], confirm_risky_actions=False, auto_approve_writes=False
    )
    assert needs_approval(Risk.WRITE, cfg)
    assert not needs_approval(Risk.EXECUTE, cfg)
    assert needs_approval(Risk.DESTRUCTIVE, cfg)
    assert needs_approval(Risk.EXTERNAL, cfg)
