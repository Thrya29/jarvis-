from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from jarvis.core.audit import AuditLog
from jarvis.core.config import LoggingConfig
from jarvis.core.instance import AlreadyRunningError, InstanceLock, load_or_create_token
from jarvis.core.logging_setup import configure_logging, redact
from jarvis.core.secrets import SecretName, delete_secret, get_secret, set_secret


def test_redacts_anthropic_keys() -> None:
    out = redact("key=sk-ant-api03-abcdefghijklmnopqrstuvwxyz")
    assert "abcdefghijkl" not in out


def test_redacts_bearer_tokens() -> None:
    out = redact('{"authorization": "Bearer abcdefghijklmnopqrstuvwxyz"}')
    assert "abcdefghijklmnop" not in out


def test_json_log_file(tmp_path: Path) -> None:
    configure_logging(LoggingConfig(), tmp_path, console=False)
    try:
        logging.getLogger("t").info("hello sk-ant-api03-secretsecretsecret", extra={"task": 7})
        for h in logging.getLogger().handlers:
            h.flush()
        lines = (tmp_path / "jarvis.log").read_text(encoding="utf-8").splitlines()
        record = json.loads(lines[-1])
        assert record["task"] == 7
        assert "secretsecret" not in record["msg"]
    finally:
        for h in logging.getLogger().handlers:
            h.close()
        logging.getLogger().handlers.clear()


def test_instance_lock_is_exclusive(tmp_path: Path) -> None:
    lock_path = tmp_path / "j.lock"
    with InstanceLock(lock_path), pytest.raises(AlreadyRunningError):
        InstanceLock(lock_path).acquire()
    with InstanceLock(lock_path):  # reacquirable after release
        pass


def test_token_is_stable(tmp_path: Path) -> None:
    p = tmp_path / "token"
    first = load_or_create_token(p)
    assert len(first) >= 32
    assert load_or_create_token(p) == first


def test_secrets_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    assert get_secret(SecretName.ANTHROPIC_API_KEY) is None
    set_secret(SecretName.ANTHROPIC_API_KEY, "  sk-ant-test  ")
    assert get_secret(SecretName.ANTHROPIC_API_KEY) == "sk-ant-test"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    assert get_secret(SecretName.ANTHROPIC_API_KEY) == "from-env"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert delete_secret(SecretName.ANTHROPIC_API_KEY)
    assert not delete_secret(SecretName.ANTHROPIC_API_KEY)


def test_empty_secret_rejected() -> None:
    with pytest.raises(ValueError):
        set_secret(SecretName.ANTHROPIC_API_KEY, "   ")


def test_audit_log_appends_redacted(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    audit.record("tool.call", tool="shell", arg="sk-ant-api03-abcdefghijklmnop")
    audit.record("tool.done")
    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["event"] for line in lines] == ["tool.call", "tool.done"]
    assert "abcdefghijklmnop" not in lines[0]
