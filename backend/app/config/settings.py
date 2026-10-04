"""Application configuration.

All configuration is environment-driven (``SAA_`` prefix, ``.env`` supported) and
validated by Pydantic. Secrets are ``SecretStr`` so they never appear in ``repr``.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _home() -> Path:
    return Path(os.path.expanduser("~"))


class LLMSettings(BaseModel):
    provider: Literal["anthropic", "openai", "none"] = "anthropic"
    model: str = "claude-sonnet-5-5"
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_tokens: int = Field(default=4096, ge=64, le=64000)
    request_timeout_s: float = 90.0
    base_url: str | None = None  # OpenAI-compatible endpoints (Ollama, vLLM, Azure gateway, ...)
    api_key: SecretStr | None = None
    # Estimated price per 1M tokens (USD). Used for *estimates* only.
    price_input_per_mtok: float = 3.0
    price_output_per_mtok: float = 15.0


class VisionSettings(BaseModel):
    enabled: bool = True
    provider: Literal["anthropic", "openai", "none"] = "anthropic"
    model: str = "claude-sonnet-5-5"
    min_confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    api_key: SecretStr | None = None
    base_url: str | None = None


class DatabaseSettings(BaseModel):
    url: str = "postgresql+asyncpg://saa:saa@localhost:5432/saa"
    echo: bool = False
    auto_migrate: bool = False  # dev convenience: run alembic upgrade head at startup


class BrowserSettings(BaseModel):
    headless: bool = True
    channel: str | None = None  # e.g. "chrome" to use the installed Chrome
    max_tabs: int = 8
    default_timeout_ms: int = 15000
    allow_private_network: bool = False  # SSRF guard: block 127.0.0.0/8, 10/8, ...
    allow_upload: bool = False
    max_download_bytes: int = 200 * 1024 * 1024
    blocked_schemes: tuple[str, ...] = (
        "file",
        "chrome",
        "chrome-extension",
        "about",
        "javascript",
        "data",
    )


class FilesystemSettings(BaseModel):
    # Roots the agent may read/write inside. Empty -> Desktop, Downloads, Documents, workspace.
    allowed_roots: list[Path] = Field(default_factory=list)
    read_only_roots: list[Path] = Field(default_factory=list)
    workspace: Path = Field(default_factory=lambda: _home() / "SmartAIAssistant" / "workspace")
    max_read_bytes: int = 2 * 1024 * 1024
    max_write_bytes: int = 10 * 1024 * 1024
    max_list_entries: int = 500
    max_search_results: int = 100
    blocked_write_extensions: tuple[str, ...] = (
        ".exe", ".dll", ".bat", ".cmd", ".ps1", ".psm1", ".vbs", ".vbe", ".js_exec", ".msi",
        ".scr", ".reg", ".lnk", ".com", ".hta", ".sys", ".cpl", ".jar",
    )  # fmt: skip


class DesktopSettings(BaseModel):
    enabled: bool = True
    max_tree_elements: int = 150
    max_tree_depth: int = 6
    # name -> executable (resolved on PATH or absolute). Only these can be launched.
    apps: dict[str, str] = Field(
        default_factory=lambda: {
            "notepad": "notepad.exe",
            "calculator": "calc.exe",
            "paint": "mspaint.exe",
            "vscode": "code",
            "explorer": "explorer.exe",
        }
    )
    # Windows the agent must never touch (credential managers, security prompts, ...).
    blocked_window_patterns: list[str] = Field(
        default_factory=lambda: [
            "windows security",
            "user account control",
            "credential manager",
            "keepass",
            "1password",
            "bitwarden",
            "lastpass",
            "registry editor",
            "group policy",
            "local security policy",
            "windows defender",
            "task scheduler",
            "services",
        ]
    )
    # Keyboard/mouse input into these processes would bypass the terminal safety layer.
    blocked_input_processes: list[str] = Field(
        default_factory=lambda: [
            "cmd.exe",
            "powershell.exe",
            "pwsh.exe",
            "windowsterminal.exe",
            "wt.exe",
            "conhost.exe",
            "bash.exe",
            "wsl.exe",
            "mintty.exe",
            "powershell_ise.exe",
            "alacritty.exe",
            "wezterm-gui.exe",
        ]
    )
    allow_coordinate_fallback: bool = True


class TerminalSettings(BaseModel):
    default_timeout_s: float = 60.0
    max_timeout_s: float = 600.0
    max_output_bytes: int = 64 * 1024
    allow_shell_interpreters: bool = False
    extra_allowed_programs: list[str] = Field(default_factory=list)


class LimitSettings(BaseModel):
    max_execution_seconds: float = 1800.0
    max_actions: int = 200
    max_llm_calls: int = 60
    max_vision_calls: int = 10
    max_retries_per_step: int = 3
    max_replans: int = 2
    max_shell_operations: int = 40
    max_adaptive_iterations: int = 12
    approval_timeout_s: float = 600.0
    max_concurrent_tasks: int = 2


class SecretsSettings(BaseModel):
    provider: Literal["env", "keyring"] = (
        "env"  # where API keys are looked up when not set explicitly
    )
    keyring_service: str = "smart-ai-assistant"


class SecuritySettings(BaseModel):
    api_token: SecretStr | None = None
    # Browser origins allowed for CORS and WebSocket handshakes (dev UI on :5173, bundled UI on :8000).
    allowed_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:8000",
            "http://127.0.0.1:8000",
        ]
    )
    allowed_hosts: list[str] = Field(
        default_factory=lambda: ["localhost", "127.0.0.1", "testserver"]
    )
    # Block (rather than only flag) tool results containing high-severity injection patterns.
    block_on_injection: bool = False


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SAA_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: Literal["dev", "test", "prod"] = "dev"
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"
    log_json: bool = True
    serve_frontend: bool = True  # serve frontend/dist from the API process when it has been built
    data_dir: Path = Field(default_factory=lambda: _home() / "SmartAIAssistant" / "data")

    llm: LLMSettings = Field(default_factory=LLMSettings)
    vision: VisionSettings = Field(default_factory=VisionSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    browser: BrowserSettings = Field(default_factory=BrowserSettings)
    filesystem: FilesystemSettings = Field(default_factory=FilesystemSettings)
    terminal: TerminalSettings = Field(default_factory=TerminalSettings)
    desktop: DesktopSettings = Field(default_factory=DesktopSettings)
    limits: LimitSettings = Field(default_factory=LimitSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    secrets: SecretsSettings = Field(default_factory=SecretsSettings)

    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    def resolved_allowed_roots(self) -> list[Path]:
        """Allowed roots, defaulting to the user's Desktop/Downloads/Documents + workspace."""
        if self.filesystem.allowed_roots:
            roots = list(self.filesystem.allowed_roots)
        else:
            home = _home()
            roots = [home / "Desktop", home / "Downloads", home / "Documents"]
        roots.append(self.filesystem.workspace)
        return roots


@lru_cache
def get_settings() -> Settings:
    return Settings()
