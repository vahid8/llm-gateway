"""Settings tests — the Docker-secret / *_FILE key loader."""
from __future__ import annotations

from app.config import Settings


def test_key_file_overrides_inline_key(tmp_path):
    secret = tmp_path / "doubleword.key"
    secret.write_text("sk-from-file\n")  # trailing newline must be stripped
    settings = Settings(
        _env_file=None,  # hermetic: ignore the developer's local .env
        doubleword_api_key="sk-inline-should-lose",
        doubleword_api_key_file=str(secret),
    )
    assert settings.doubleword_api_key == "sk-from-file"


def test_missing_key_file_leaves_inline_key(tmp_path):
    settings = Settings(
        _env_file=None,
        doubleword_api_key="sk-inline",
        doubleword_api_key_file=str(tmp_path / "does-not-exist.key"),
    )
    assert settings.doubleword_api_key == "sk-inline"


def test_empty_key_file_does_not_clobber(tmp_path):
    secret = tmp_path / "empty.key"
    secret.write_text("   \n")
    settings = Settings(
        _env_file=None,
        anthropic_api_key="sk-inline",
        anthropic_api_key_file=str(secret),
    )
    assert settings.anthropic_api_key == "sk-inline"
