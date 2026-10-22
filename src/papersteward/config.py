"""应用配置：从环境变量与 .env 读取，校验后供 API、CLI 与任务复用。"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_PREFIX = "PAPERSTEWARD_"

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
_VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


class AppEnv(StrEnum):
    """运行环境；prod 不改变安全默认值，只用于区分行为。"""

    DEV = "dev"
    TEST = "test"
    PROD = "prod"


class LogFormat(StrEnum):
    """日志输出格式；json 用于采集与回放，console 用于本地阅读。"""

    JSON = "json"
    CONSOLE = "console"


class Settings(BaseSettings):
    """运行配置。所有字段都可用 ``PAPERSTEWARD_<FIELD>`` 覆盖。"""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        validate_default=True,
    )

    app_env: AppEnv = AppEnv.DEV
    data_dir: Path = Path("var")
    database_url: str | None = None
    log_level: LogLevel = "INFO"
    log_format: LogFormat = LogFormat.JSON
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)
    timezone: str = "Asia/Shanghai"
    semantic_scholar_api_key: SecretStr | None = None

    @field_validator("semantic_scholar_api_key", mode="before")
    @classmethod
    def _empty_source_key(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip().upper()
        if value not in _VALID_LOG_LEVELS:
            raise ValueError("必须是 DEBUG / INFO / WARNING / ERROR / CRITICAL 之一")
        return value

    @field_validator("data_dir")
    @classmethod
    def _resolve_data_dir(cls, value: Path) -> Path:
        path = value.expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        return path.resolve()

    @field_validator("database_url", mode="before")
    @classmethod
    def _empty_database_url_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("api_host")
    @classmethod
    def _validate_api_host(cls, value: str) -> str:
        host = value.strip()
        if not host:
            raise ValueError("不能为空")
        if "://" in host:
            raise ValueError("只填写主机名或 IP，不要包含协议前缀")
        return host

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        timezone = value.strip()
        if not timezone:
            raise ValueError("不能为空")
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("必须是合法的 IANA 时区，例如 Asia/Shanghai") from exc
        return timezone

    @property
    def db_url(self) -> str:
        """SQLAlchemy 连接串；未显式配置时落在 data_dir 下。"""
        if self.database_url:
            return self.database_url
        return f"sqlite+pysqlite:///{self.data_dir / 'papersteward.db'}"

    def ensure_data_dir(self) -> Path:
        """创建数据目录并返回路径；其他模块在首次写盘前调用。"""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir


def format_settings_error(exc: ValidationError) -> str:
    """把 pydantic 校验错误转成可直接展示的环境变量提示。"""
    lines = ["配置校验失败，请检查以下环境变量："]
    for error in exc.errors():
        location = error.get("loc", ())
        field = str(location[0]) if location else "<root>"
        env_name = f"{ENV_PREFIX}{field.upper()}"
        message = error.get("msg", "无效值")
        lines.append(f"- {env_name}: {message}")
    return "\n".join(lines)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程级配置单例；测试可调用 ``get_settings.cache_clear()`` 重置。"""
    return Settings()


__all__ = [
    "ENV_PREFIX",
    "AppEnv",
    "LogFormat",
    "LogLevel",
    "Settings",
    "format_settings_error",
    "get_settings",
]
