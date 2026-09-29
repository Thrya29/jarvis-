from __future__ import annotations

import pytest

from jarvis import __version__
from jarvis.cli import main, run_checks
from jarvis.core.paths import AppPaths


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["version"]) == 0
    assert __version__ in capsys.readouterr().out


def test_config_init_and_show(paths: AppPaths, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["config", "init"]) == 0
    assert paths.config_file.exists()
    assert main(["config", "show"]) == 0
    assert '"provider": "anthropic"' in capsys.readouterr().out


def test_doctor_flags_missing_key(paths: AppPaths) -> None:
    checks = {c.name: c for c in run_checks(paths)}
    assert checks["config"].ok
    assert not checks["anthropic key"].ok and checks["anthropic key"].required
    assert not checks["ollama"].required


def test_doctor_passes_with_key(paths: AppPaths, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    checks = {c.name: c for c in run_checks(paths)}
    assert checks["anthropic key"].ok


def test_invalid_config_exit_code(paths: AppPaths) -> None:
    paths.config_file.write_text("bogus = 1\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        main(["config", "show"])
    assert exc.value.code == 2
