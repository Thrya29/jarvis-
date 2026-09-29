"""Secret storage backed by Windows Credential Manager (via ``keyring``).

Environment variables take precedence so CI and headless deployments work
without a credential store.
"""

from __future__ import annotations

import os
from enum import StrEnum

import keyring
import keyring.errors

SERVICE = "jarvis"


class SecretName(StrEnum):
    ANTHROPIC_API_KEY = "ANTHROPIC_API_KEY"


def get_secret(name: SecretName) -> str | None:
    env = os.environ.get(name.value)
    if env:
        return env
    try:
        value: str | None = keyring.get_password(SERVICE, name.value)
    except keyring.errors.KeyringError:
        return None
    return value or None


def set_secret(name: SecretName, value: str) -> None:
    if not value.strip():
        raise ValueError("secret value must not be empty")
    keyring.set_password(SERVICE, name.value, value.strip())


def delete_secret(name: SecretName) -> bool:
    try:
        keyring.delete_password(SERVICE, name.value)
    except keyring.errors.PasswordDeleteError:
        return False
    return True
