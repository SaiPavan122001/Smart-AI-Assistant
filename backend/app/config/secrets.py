"""Secret-provider abstraction.

API keys are resolved through a ``SecretProvider`` so they can come from the environment (default)
or an OS credential store without the rest of the code knowing where they live. Values are only ever
held as ``SecretStr`` / local variables and never logged.
"""

from __future__ import annotations

import os
from typing import Protocol

from app.utils.errors import AgentError, ErrorCode


class SecretProvider(Protocol):
    name: str

    def get(self, key: str) -> str | None:
        """Return the secret stored under ``key`` (e.g. ``ANTHROPIC_API_KEY``) or ``None``."""
        ...


class EnvSecretProvider:
    """Reads secrets from environment variables (the default)."""

    name = "env"

    def get(self, key: str) -> str | None:
        return os.environ.get(key) or None


class KeyringSecretProvider:
    """Reads secrets from the OS credential store (Windows Credential Manager via ``keyring``).

    Store a secret with: ``python -m keyring set smart-ai-assistant ANTHROPIC_API_KEY``.
    """

    name = "keyring"

    def __init__(self, service: str = "smart-ai-assistant") -> None:
        try:
            import keyring
        except ImportError as exc:
            raise AgentError(
                "secrets.provider=keyring requires the 'keyring' package (pip install keyring)",
                code=ErrorCode.NOT_AVAILABLE,
            ) from exc
        self._keyring = keyring
        self._service = service

    def get(self, key: str) -> str | None:
        try:
            value = self._keyring.get_password(self._service, key)
        except Exception:  # a locked/unavailable credential store must not crash startup
            return None
        return value or None


class ChainedSecretProvider:
    """Tries providers in order (e.g. keyring first, then environment)."""

    name = "chain"

    def __init__(self, *providers: SecretProvider) -> None:
        self._providers = providers

    def get(self, key: str) -> str | None:
        for p in self._providers:
            value = p.get(key)
            if value:
                return value
        return None


def build_secret_provider(
    kind: str, *, keyring_service: str = "smart-ai-assistant"
) -> SecretProvider:
    if kind == "keyring":
        return ChainedSecretProvider(KeyringSecretProvider(keyring_service), EnvSecretProvider())
    return EnvSecretProvider()
