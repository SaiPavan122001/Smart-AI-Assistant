"""Build LLM / vision providers from configuration through the provider registry."""

from __future__ import annotations

from app.config.secrets import SecretProvider, build_secret_provider
from app.config.settings import Settings
from app.llm.base import LLMProvider
from app.llm.registry import ProviderConfig, get_provider_spec
from app.utils.errors import AgentError, ErrorCode


def _resolve_key(
    spec_key_env: tuple[str, ...], explicit: str | None, secrets: SecretProvider
) -> str:
    if explicit:
        return explicit
    for env in spec_key_env:
        if value := secrets.get(env):
            return value
    return ""


def build_provider(
    name: str, *, model: str, api_key: str | None, base_url: str | None, timeout_s: float,
    overrides: dict[str, bool], app_name: str, app_url: str | None, secrets: SecretProvider,
) -> LLMProvider | None:  # fmt: skip
    """Build one provider, or return ``None`` when it is unconfigured (no key). Raises for impossible configs."""
    if name == "none":
        return None
    spec = get_provider_spec(name)
    if spec.requires_base_url and not base_url:
        raise AgentError(
            f"Provider '{name}' requires a base URL (SAA_LLM__BASE_URL)",
            code=ErrorCode.INVALID_INPUT,
        )
    key = _resolve_key(spec.key_env, api_key, secrets)
    if spec.requires_key and not key:
        return None
    return spec.builder(
        ProviderConfig(
            model=model,
            api_key=key,
            base_url=base_url,
            timeout_s=timeout_s,
            capability_overrides=overrides,
            app_name=app_name,
            app_url=app_url,
        )
    )


def build_llm_provider(
    settings: Settings, secrets: SecretProvider | None = None
) -> LLMProvider | None:
    """The configured agent model, or ``None`` when no provider/key is configured (tasks then fail with a clear message)."""
    secrets = secrets or build_secret_provider(
        settings.secrets.provider, keyring_service=settings.secrets.keyring_service
    )
    c = settings.llm
    return build_provider(
        c.provider, model=c.model, api_key=c.api_key.get_secret_value() if c.api_key else None, base_url=c.base_url,
        timeout_s=c.request_timeout_s, overrides=c.capabilities, app_name=c.app_name, app_url=c.app_url, secrets=secrets,
    )  # fmt: skip


def build_vision_provider(
    settings: Settings, secrets: SecretProvider | None = None
) -> LLMProvider | None:
    secrets = secrets or build_secret_provider(
        settings.secrets.provider, keyring_service=settings.secrets.keyring_service
    )
    v, c = settings.vision, settings.llm
    key = v.api_key.get_secret_value() if v.api_key else None
    if key is None and v.provider == c.provider and c.api_key:  # same vendor: reuse the agent key
        key = c.api_key.get_secret_value()
    return build_provider(
        v.provider, model=v.model, api_key=key, base_url=v.base_url or (c.base_url if v.provider == c.provider else None),
        timeout_s=c.request_timeout_s, overrides={}, app_name=c.app_name, app_url=c.app_url, secrets=secrets,
    )  # fmt: skip
