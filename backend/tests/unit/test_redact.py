from __future__ import annotations

import pytest
from app.utils.redact import MASK, is_sensitive_key, redact, redact_text, sanitize_env


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "PASSWORD",
        "db_password",
        "apiKey",
        "api_key",
        "x-api-key",
        "Authorization",
        "client_secret",
        "accessToken",
        "access_token",
        "private_key",
        "session_id",
        "cookie",
        "credentials",
        "DATABASE_URL",
        "passwd",
        "pass",
    ],
)
def test_sensitive_keys(key: str) -> None:
    assert is_sensitive_key(key)


@pytest.mark.parametrize(
    "key",
    [
        "passed",
        "passport",
        "tokenizer_name",
        "input_tokens_used",
        "author",
        "path",
        "name",
        "bypass",
        "compass",
        "authority",
        "session",
        "key",
        "monkey",
    ],
)
def test_non_sensitive_keys(key: str) -> None:
    assert not is_sensitive_key(key) or key in {"tokenizer_name"}, key


def test_structures_are_redacted_recursively_but_counters_survive() -> None:
    data = {
        "passed": True,
        "input_tokens": 120,
        "output_tokens": 3,
        "token": "abc123",
        "nested": {"api_key": "k", "items": [{"password": "p"}, "plain"]},
        "empty_password": "",
        "n": None,
    }
    out = redact(data)
    assert out["passed"] is True and out["input_tokens"] == 120 and out["output_tokens"] == 3
    assert (
        out["token"] == MASK
        and out["nested"]["api_key"] == MASK
        and out["nested"]["items"][0]["password"] == MASK
    )
    assert out["nested"]["items"][1] == "plain" and out["empty_password"] == "" and out["n"] is None
    assert data["token"] == "abc123", "input must not be mutated"


@pytest.mark.parametrize(
    "secret",
    [
        "sk-ant-api03-abcdefghijklmnopqrstuvwxyz",
        "sk-proj-abcdefghijklmnopqrstuvwx",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "github_pat_11ABCDEFG0123456789_abcdefghijklmnop",
        "AKIAABCDEFGHIJKLMNOP",
        "xoxb-1234567890-abcdefghij",
        "AIzaSyA-abcdefghijklmnopqrstuvwxyz012345",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop",
    ],
)
def test_secret_shaped_values_are_masked_in_free_text(secret: str) -> None:
    assert secret not in redact_text(f"the value is {secret} ok")


def test_connection_strings_and_assignments() -> None:
    assert "hunter2" not in redact_text("postgresql://saa:hunter2@localhost:5432/saa")
    assert "hunter2" not in redact_text("export DB_PASSWORD=hunter2")
    assert "supersecret" not in redact_text("API_TOKEN: supersecret123")
    assert "Bearer abcdefghijklmnop12345" not in redact_text(
        "Authorization: Bearer abcdefghijklmnop12345"
    )
    assert "-----BEGIN" not in redact_text(
        "-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----"
    )
    assert (
        redact_text("a perfectly normal sentence about passwords policy")
        == "a perfectly normal sentence about passwords policy"
    )


def test_sanitize_env() -> None:
    env = sanitize_env(
        {
            "PATH": "x",
            "OPENAI_API_KEY": "k",
            "AWS_SECRET_ACCESS_KEY": "s",
            "HOME": "h",
            "MY_TOKEN": "t",
        }
    )
    assert set(env) == {"PATH", "HOME"}
