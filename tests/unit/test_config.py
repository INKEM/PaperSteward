from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from papersteward.config import (
    AppEnv,
    LLMProviderKind,
    LogFormat,
    Settings,
    format_settings_error,
    get_settings,
)


def test_defaults_are_local_and_safe() -> None:
    settings = Settings(_env_file=None)

    assert settings.app_env is AppEnv.DEV
    assert settings.log_level == "INFO"
    assert settings.log_format is LogFormat.JSON
    assert settings.api_host == "127.0.0.1"
    assert settings.api_port == 8000
    assert settings.timezone == "Asia/Shanghai"
    assert settings.database_url is None
    assert settings.data_dir.is_absolute()


def test_env_vars_override_and_normalize(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAPERSTEWARD_APP_ENV", "prod")
    monkeypatch.setenv("PAPERSTEWARD_LOG_LEVEL", "debug")
    monkeypatch.setenv("PAPERSTEWARD_LOG_FORMAT", "console")
    monkeypatch.setenv("PAPERSTEWARD_API_HOST", "localhost")
    monkeypatch.setenv("PAPERSTEWARD_API_PORT", "9001")
    monkeypatch.setenv("PAPERSTEWARD_TIMEZONE", "UTC")

    settings = Settings(_env_file=None)

    assert settings.app_env is AppEnv.PROD
    assert settings.log_level == "DEBUG"
    assert settings.log_format is LogFormat.CONSOLE
    assert settings.api_host == "localhost"
    assert settings.api_port == 9001
    assert settings.timezone == "UTC"


def test_env_file_is_loaded(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "PAPERSTEWARD_API_PORT=9100\nPAPERSTEWARD_LOG_FORMAT=console\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)

    assert settings.api_port == 9100
    assert settings.log_format is LogFormat.CONSOLE


def test_env_example_is_valid() -> None:
    env_example = Path(__file__).resolve().parents[2] / ".env.example"

    settings = Settings(_env_file=env_example)

    assert settings.api_host == "127.0.0.1"
    assert settings.api_port == 8000
    assert settings.log_format is LogFormat.JSON
    assert settings.data_dir.is_absolute()


def test_database_url_is_derived_from_data_dir(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path)

    assert settings.db_url == f"sqlite+pysqlite:///{tmp_path / 'papersteward.db'}"


def test_explicit_database_url_wins() -> None:
    settings = Settings(
        _env_file=None,
        database_url="sqlite+pysqlite:///custom.db",
    )

    assert settings.db_url == "sqlite+pysqlite:///custom.db"


def test_blank_database_url_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAPERSTEWARD_DATABASE_URL", "   ")

    settings = Settings(_env_file=None)

    assert settings.database_url is None
    assert settings.db_url.endswith("papersteward.db")


def test_relative_data_dir_resolves_against_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)

    settings = Settings(_env_file=None, data_dir=Path("data"))

    assert settings.data_dir == (tmp_path / "data").resolve()


def test_ensure_data_dir_creates_nested_path(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "data"
    settings = Settings(_env_file=None, data_dir=target)

    result = settings.ensure_data_dir()

    assert result == target.resolve()
    assert target.is_dir()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("PAPERSTEWARD_LOG_LEVEL", "LOUD"),
        ("PAPERSTEWARD_LOG_FORMAT", "yaml"),
        ("PAPERSTEWARD_TIMEZONE", "Mars/Olympus"),
        ("PAPERSTEWARD_APP_ENV", "staging"),
        ("PAPERSTEWARD_API_PORT", "70000"),
        ("PAPERSTEWARD_API_HOST", ""),
        ("PAPERSTEWARD_API_HOST", "http://127.0.0.1"),
    ],
)
def test_invalid_env_is_rejected_with_actionable_message(
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    value: str,
) -> None:
    monkeypatch.setenv(key, value)

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)

    message = format_settings_error(exc_info.value)
    assert message.startswith("配置校验失败")
    assert key in message


def test_invalid_env_file_points_to_variable(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("PAPERSTEWARD_API_PORT=0\n", encoding="utf-8")

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=env_file)

    assert "PAPERSTEWARD_API_PORT" in format_settings_error(exc_info.value)


def test_get_settings_is_cached() -> None:
    get_settings.cache_clear()

    first = get_settings()
    second = get_settings()

    assert first is second


def test_llm_defaults_are_offline_and_key_is_optional() -> None:
    settings = Settings(_env_file=None)

    assert settings.llm_provider is LLMProviderKind.FAKE
    assert settings.llm_base_url == "https://api.openai.com/v1"
    assert settings.llm_model is None
    assert settings.llm_api_key is None
    assert settings.llm_timeout_s == 30.0
    assert settings.llm_max_attempts == 3
    assert settings.llm_prompt_cost_per_1k_usd == 0.0
    assert settings.llm_completion_cost_per_1k_usd == 0.0


def test_llm_env_overrides_and_provider_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAPERSTEWARD_LLM_PROVIDER", "openai")
    monkeypatch.setenv("PAPERSTEWARD_LLM_MODEL", " test-model ")
    monkeypatch.setenv("PAPERSTEWARD_LLM_API_KEY", "sk-secret-value")
    monkeypatch.setenv("PAPERSTEWARD_LLM_BASE_URL", "https://llm.example/v1/")
    monkeypatch.setenv("PAPERSTEWARD_LLM_TIMEOUT_S", "12.5")
    monkeypatch.setenv("PAPERSTEWARD_LLM_MAX_ATTEMPTS", "4")
    monkeypatch.setenv("PAPERSTEWARD_LLM_PROMPT_COST_PER_1K_USD", "0.1")
    monkeypatch.setenv("PAPERSTEWARD_LLM_COMPLETION_COST_PER_1K_USD", "0.2")

    settings = Settings(_env_file=None)

    assert settings.llm_provider is LLMProviderKind.OPENAI_COMPATIBLE
    assert settings.llm_model == "test-model"
    assert settings.llm_base_url == "https://llm.example/v1"
    assert settings.llm_timeout_s == 12.5
    assert settings.llm_max_attempts == 4
    assert settings.llm_prompt_cost_per_1k_usd == 0.1
    assert settings.llm_completion_cost_per_1k_usd == 0.2
    assert settings.llm_api_key is not None
    assert settings.llm_api_key.get_secret_value() == "sk-secret-value"
    assert "sk-secret-value" not in repr(settings)


def test_llm_blank_values_fall_back_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAPERSTEWARD_LLM_API_KEY", "   ")
    monkeypatch.setenv("PAPERSTEWARD_LLM_MODEL", "   ")

    settings = Settings(_env_file=None)

    assert settings.llm_api_key is None
    assert settings.llm_model is None


def test_llm_openai_provider_requires_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAPERSTEWARD_LLM_PROVIDER", "openai_compatible")

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)

    message = format_settings_error(exc_info.value)
    assert "PAPERSTEWARD_LLM_MODEL" in message


@pytest.mark.parametrize(
    "value",
    [
        "http://llm.example/v1",
        "https://user:pass@llm.example/v1",
        "https://llm.example/v1?token=secret",
        "not-a-url",
    ],
)
def test_llm_base_url_rejects_insecure_or_embedded_secrets(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("PAPERSTEWARD_LLM_BASE_URL", value)

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)

    assert "PAPERSTEWARD_LLM_BASE_URL" in format_settings_error(exc_info.value)


def test_llm_base_url_allows_loopback_http(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAPERSTEWARD_LLM_BASE_URL", "http://127.0.0.1:11434/v1")

    settings = Settings(_env_file=None)

    assert settings.llm_base_url == "http://127.0.0.1:11434/v1"
