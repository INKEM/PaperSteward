"""LLM 设置 API：浏览器配置模型服务，保存即授权。"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from papersteward.api.deps import SessionDep
from papersteward.config import get_settings
from papersteward.errors import InvalidRequestError
from papersteward.llm.errors import LLMError
from papersteward.llm.gateway import LLMGateway
from papersteward.llm.openai_compatible import OpenAICompatibleProvider
from papersteward.llm.runtime import (
    LLMSettingsPayload,
    resolve_llm_config,
    save_llm_config,
)

router = APIRouter(prefix="/settings", tags=["settings"])


class LLMSettingsUpdate(LLMSettingsPayload):
    """PUT 请求体：与表单校验共用同一契约。"""


class LLMSettingsStatus(BaseModel):
    configured: bool
    source: Literal["db", "env"]
    base_url: str
    model: str | None
    api_key_set: bool
    api_key_hint: str | None
    api_key_source: Literal["db", "env"] | None
    timeout_s: float
    ready: bool
    problem: str | None
    updated_at: str | None = None


def _status(session: SessionDep) -> LLMSettingsStatus:
    settings = get_settings()
    config = resolve_llm_config(session, settings)
    key_value = config.api_key.get_secret_value() if config.api_key else ""
    hint = f"****{key_value[-4:]}" if len(key_value) >= 4 else ("****" if key_value else None)
    return LLMSettingsStatus(
        configured=config.source == "db",
        source=config.source,
        base_url=config.base_url,
        model=config.model,
        api_key_set=config.has_api_key,
        api_key_hint=hint,
        api_key_source=(
            None if not config.has_api_key else ("env" if config.api_key_from_env else "db")
        ),
        timeout_s=config.timeout_s,
        ready=config.ready,
        problem=config.readiness_problem(),
        updated_at=config.updated_at.isoformat() if config.updated_at else None,
    )


@router.get("/llm", response_model=LLMSettingsStatus)
def read_llm_settings(session: SessionDep) -> LLMSettingsStatus:
    return _status(session)


@router.put("/llm", response_model=LLMSettingsStatus)
def update_llm_settings(body: LLMSettingsUpdate, session: SessionDep) -> LLMSettingsStatus:
    with session.begin():
        save_llm_config(session, body, get_settings())
    return _status(session)


class LLMTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str = Field(min_length=1, max_length=2048)
    api_key: str | None = Field(default=None, max_length=4096)
    model: str = Field(min_length=1, max_length=128)
    # 与保存端点（LLMSettingsPayload le=600）和设置表单 max=600 保持一致。
    timeout_s: float = Field(default=30.0, gt=0, le=600)


class LLMTestResponse(BaseModel):
    ok: bool
    model_id: str | None = None
    latency_ms: float | None = None
    message: str


@router.post("/llm/test", response_model=LLMTestResponse)
def test_llm_settings(body: LLMTestRequest, session: SessionDep) -> LLMTestResponse:
    """用表单值即时发起一次最小补全请求；不落库。留空密钥时沿用已保存的密钥。"""
    settings = get_settings()
    api_key = body.api_key
    if not (api_key and api_key.strip()):
        api_key = None
        saved = resolve_llm_config(session, settings)
        if saved.api_key is not None and saved.base_url == body.base_url.strip().rstrip("/"):
            api_key = saved.api_key.get_secret_value()
    try:
        payload = LLMSettingsPayload.model_validate(
            {
                "base_url": body.base_url,
                "api_key": api_key or "",
                "model": body.model,
                "timeout_s": body.timeout_s,
                "consent": True,
            }
        )
    except ValidationError as exc:
        detail = "；".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        raise InvalidRequestError(f"配置无效：{detail}") from exc
    provider = OpenAICompatibleProvider(
        base_url=payload.base_url,
        model=payload.model,
        api_key=payload.api_key,
        timeout_s=payload.timeout_s,
    )
    try:
        gateway = LLMGateway(provider, max_attempts=1, timeout_s=payload.timeout_s)
        result = gateway.ping()
    except (LLMError, ValueError) as exc:
        return LLMTestResponse(ok=False, message=f"连接失败：{exc}")
    finally:
        provider.close()
    return LLMTestResponse(
        ok=True,
        model_id=result.model_id,
        latency_ms=round(result.usage.latency_ms, 1),
        message=f"连接成功（{result.model_id}）",
    )


__all__ = ["router"]
