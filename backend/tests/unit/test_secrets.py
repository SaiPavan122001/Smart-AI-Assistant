from __future__ import annotations

import pytest
from app.config.secrets import (
    ChainedSecretProvider,
    EnvSecretProvider,
    KeyringSecretProvider,
    build_secret_provider,
)
from app.config.settings import LLMSettings, SecretsSettings, Settings
from app.llm.anthropic import AnthropicProvider
from app.llm.factory import build_llm_provider
from app.utils.errors import AgentError


class Fake:
    name = "fake"

    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def get(self, key: str) -> str | None:
        return self.values.get(key)


def test_env_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOME_SECRET", "abc")
    monkeypatch.delenv("MISSING_SECRET", raising=False)
    p = EnvSecretProvider()
    assert p.get("SOME_SECRET") == "abc" and p.get("MISSING_SECRET") is None


def test_chain_prefers_first_provider() -> None:
    chain = ChainedSecretProvider(Fake({"K": "from-store"}), Fake({"K": "from-env", "J": "j"}))
    assert chain.get("K") == "from-store" and chain.get("J") == "j" and chain.get("Z") is None


def test_factory_resolves_keys_through_the_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    settings = Settings(llm=LLMSettings(provider="anthropic"))
    assert build_llm_provider(settings, Fake({})) is None
    provider = build_llm_provider(
        settings, Fake({"ANTHROPIC_API_KEY": "sk-ant-from-keyring-1234567890"})
    )
    assert isinstance(provider, AnthropicProvider)
    # an explicit setting wins over the provider
    explicit = Settings(llm=LLMSettings(provider="anthropic", api_key="sk-ant-explicit-1234567890"))  # type: ignore[arg-type]
    assert isinstance(build_llm_provider(explicit, Fake({})), AnthropicProvider)


def test_keyring_provider_degrades_clearly(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def no_keyring(name, *a, **k):  # type: ignore[no-untyped-def]
        if name == "keyring":
            raise ImportError("no keyring")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_keyring)
    with pytest.raises(AgentError, match="keyring"):
        KeyringSecretProvider()
    assert isinstance(build_secret_provider("env"), EnvSecretProvider)
    assert Settings(secrets=SecretsSettings(provider="keyring")).secrets.provider == "keyring"
