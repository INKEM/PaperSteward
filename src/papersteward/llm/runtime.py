"""LLM 运行时配置：数据库配置优先，环境变量回退。

浏览器设置页保存的配置以版本化 config 行（key=``llm_config``）落库；保存即代表
用户明确同意把论文标题/摘要发送给所配模型服务，因此 ``authorized=True``。环境
变量路径保持原有 CLI 语义：必须显式 ``--live`` 才发起真实调用。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy.orm import Session

from papersteward.config import (
    DEFAULT_LLM_TIMEOUT_S,
    LOOPBACK_LLM_HOSTS,
    LLMProviderKind,
    Settings,
    validate_llm_base_url,
)
from papersteward.core.enums import AssignedBy
from papersteward.errors import InvalidRequestError
from papersteward.storage.repositories import ConfigRepository

CONFIG_KEY = "llm_config"


class LLMSettingsPayload(BaseModel):
    """设置页表单的校验契约；与 Settings 的 LLM 字段保持同一规则。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    provider: LLMProviderKind = LLMProviderKind.OPENAI_COMPATIBLE
    base_url: str
    api_key: SecretStr | None = None
    model: str
    timeout_s: float = Field(default=DEFAULT_LLM_TIMEOUT_S, gt=0, le=600)
    consent: bool = Field(default=False, strict=True)

    @field_validator("provider", mode="before")
    @classmethod
    def _normalize_provider(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip().lower().replace("-", "_")
            if normalized in {"openai", "openai_compatible"}:
                return LLMProviderKind.OPENAI_COMPATIBLE
        return value

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, value: str) -> str:
        return validate_llm_base_url(value)

    @field_validator("api_key", mode="before")
    @classmethod
    def _empty_key_to_none(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @model_validator(mode="after")
    def _consent_required(self) -> LLMSettingsPayload:
        if self.provider is LLMProviderKind.FAKE:
            raise ValueError("设置页只支持 OpenAI 兼容服务，请填写真实模型地址")
        if not self.consent:
            raise ValueError("保存配置即表示同意数据外发，请勾选知情同意")
        return self


@dataclass(frozen=True, slots=True)
class LLMRuntimeConfig:
    """一次进程内解析出的 LLM 接入配置。"""

    provider: LLMProviderKind
    base_url: str
    model: str | None
    api_key: SecretStr | None
    timeout_s: float
    max_attempts: int
    prompt_cost_per_1k_usd: float
    completion_cost_per_1k_usd: float
    authorized: bool
    source: Literal["db", "env"]
    updated_at: datetime | None = None
    api_key_from_env: bool = False
    cache_enabled: bool = True
    concurrency: int = 4

    @classmethod
    def from_settings(cls, settings: Settings) -> LLMRuntimeConfig:
        return cls(
            provider=settings.llm_provider,
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            api_key=settings.llm_api_key,
            timeout_s=settings.llm_timeout_s,
            max_attempts=settings.llm_max_attempts,
            prompt_cost_per_1k_usd=settings.llm_prompt_cost_per_1k_usd,
            completion_cost_per_1k_usd=settings.llm_completion_cost_per_1k_usd,
            authorized=False,
            source="env",
            api_key_from_env=settings.llm_api_key is not None,
            cache_enabled=settings.llm_cache_enabled,
            concurrency=settings.llm_concurrency,
        )

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key and self.api_key.get_secret_value().strip())

    @property
    def requires_api_key(self) -> bool:
        """非本机地址必须携带密钥；本机 loopback 允许留空。"""
        return urlparse(self.base_url).hostname not in LOOPBACK_LLM_HOSTS

    @property
    def ready(self) -> bool:
        """真实模型调用是否已就绪：已授权、兼容服务、模型名与远端密钥齐全。"""
        return (
            self.authorized
            and self.provider is LLMProviderKind.OPENAI_COMPATIBLE
            and bool(self.model and self.model.strip())
            and (not self.requires_api_key or self.has_api_key)
        )

    def readiness_problem(self) -> str | None:
        """返回不可用原因；就绪时返回 None。文案面向浏览器用户。"""
        if self.ready:
            return None
        if self.provider is LLMProviderKind.FAKE:
            return "尚未配置模型服务。请在「设置」页填写 API 地址、密钥与模型名。"
        if not self.authorized:
            return (
                "模型配置来自环境变量，尚未在「设置」页保存授权。"
                "请在「设置」页填写并保存，或使用 CLI --live。"
            )
        if not (self.model and self.model.strip()):
            return "模型配置缺少模型名。请在「设置」页补充后保存。"
        if self.requires_api_key and not self.has_api_key:
            return (
                "模型地址不是本机，但工作区还没有保存 API 密钥；"
                "请到「设置」页填写密钥并保存，再用「测试连接」验证。"
            )
        return "模型配置不完整。请在「设置」页检查 API 地址与密钥。"


def _origin(base_url: str) -> tuple[str, str, int]:
    parsed = urlparse(base_url)
    default_port = 443 if parsed.scheme == "https" else 80
    return (
        parsed.scheme,
        parsed.hostname or "",
        parsed.port or default_port,
    )


def _env_key_for(settings: Settings, base_url: str) -> SecretStr | None:
    """同一 origin 的环境变量密钥可回退；避免把密钥发给另一个服务。"""
    if settings.llm_api_key is None:
        return None
    if _origin(settings.llm_base_url) != _origin(base_url):
        return None
    return settings.llm_api_key


def _payload_to_config(
    payload: LLMSettingsPayload,
    settings: Settings,
    *,
    authorized: bool,
    updated_at: datetime | None = None,
    api_key_from_env: bool = False,
) -> LLMRuntimeConfig:
    return LLMRuntimeConfig(
        provider=payload.provider,
        base_url=payload.base_url,
        model=payload.model,
        api_key=payload.api_key,
        timeout_s=payload.timeout_s,
        max_attempts=settings.llm_max_attempts,
        prompt_cost_per_1k_usd=settings.llm_prompt_cost_per_1k_usd,
        completion_cost_per_1k_usd=settings.llm_completion_cost_per_1k_usd,
        authorized=authorized,
        source="db",
        updated_at=updated_at,
        api_key_from_env=api_key_from_env,
        cache_enabled=settings.llm_cache_enabled,
        concurrency=settings.llm_concurrency,
    )


def resolve_llm_config(session: Session, settings: Settings) -> LLMRuntimeConfig:
    """数据库最新配置优先；缺失或损坏时回退到环境变量。"""
    row = ConfigRepository(session).latest(CONFIG_KEY)
    if row is not None:
        try:
            payload = LLMSettingsPayload.model_validate(dict(row.value_json))
            env_key = _env_key_for(settings, payload.base_url) if payload.api_key is None else None
            if env_key is not None:
                payload = payload.model_copy(update={"api_key": env_key})
            return _payload_to_config(
                payload,
                settings,
                authorized=True,
                updated_at=row.updated_at,
                api_key_from_env=env_key is not None,
            )
        except ValidationError:
            pass  # 手工改库导致的坏数据不应阻塞应用；回退 env 并由设置页提示。
    return LLMRuntimeConfig.from_settings(settings)


def save_llm_config(
    session: Session, payload: LLMSettingsPayload, settings: Settings
) -> LLMRuntimeConfig:
    """校验后的配置以新版本追加写入；返回解析后的运行时配置。

    表单留空密钥且数据库已有密钥时沿用旧值，避免每次保存都重复粘贴。
    """
    from uuid import uuid4

    from papersteward.core.entities import Config, Event, utc_now
    from papersteward.core.enums import EventType
    from papersteward.storage.repositories import EventRepository

    repository = ConfigRepository(session)
    previous = repository.latest(CONFIG_KEY)
    stored: dict[str, Any] = {
        "provider": payload.provider.value,
        "base_url": payload.base_url,
        "api_key": payload.api_key.get_secret_value() if payload.api_key else None,
        "model": payload.model,
        "timeout_s": payload.timeout_s,
        "consent": payload.consent,
    }
    api_key_from_env = False
    if not stored["api_key"] and previous is not None:
        existing = dict(previous.value_json).get("api_key")
        if existing:
            stored["api_key"] = existing
    if not stored["api_key"]:
        env_key = _env_key_for(settings, stored["base_url"])
        if env_key is not None:
            stored["api_key"] = env_key.get_secret_value()
            api_key_from_env = True
    if urlparse(stored["base_url"]).hostname not in LOOPBACK_LLM_HOSTS and not stored["api_key"]:
        raise InvalidRequestError(
            "该地址不是本机，必须填写 API 密钥；请粘贴密钥后重新保存，并先用「测试连接」验证。"
        )
    payload = LLMSettingsPayload.model_validate({**stored, "consent": True})
    version = f"v-{uuid4().hex[:16]}"
    updated_at = utc_now()
    repository.add(
        Config(
            key=CONFIG_KEY,
            version=version,
            value_json=stored,
            updated_by=AssignedBy.USER,
            updated_at=updated_at,
        )
    )
    EventRepository(session).append(
        Event(
            id=f"evt-llm-config-{uuid4().hex[:20]}",
            event_type=EventType.ONBOARDING_LLM_CONFIGURED,
            created_at=updated_at,
            payload_json={
                "version": version,
                "provider": stored["provider"],
                "base_url": stored["base_url"],
                "model": stored["model"],
                "consent": stored["consent"],
                "api_key_source": (
                    "env" if api_key_from_env else ("db" if stored["api_key"] else "none")
                ),
            },
        )
    )
    return _payload_to_config(
        payload,
        settings,
        authorized=True,
        updated_at=updated_at,
        api_key_from_env=api_key_from_env,
    )


def clear_db_llm_config(session: Session) -> bool:
    """删除数据库中的 LLM 配置（回退到环境变量）；测试与排障用。"""
    from sqlalchemy import delete

    from papersteward.storage.models import ConfigRow

    result = session.execute(delete(ConfigRow).where(ConfigRow.key == CONFIG_KEY))
    return bool(getattr(result, "rowcount", 0))


__all__ = [
    "CONFIG_KEY",
    "LLMRuntimeConfig",
    "LLMSettingsPayload",
    "clear_db_llm_config",
    "resolve_llm_config",
    "save_llm_config",
]
