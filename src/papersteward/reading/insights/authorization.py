"""阅读分析的用途授权：授权、撤回与发送边界核对；独立于模型连接 consent。

授权绑定当前工作区（同库即工作区）、用途、规范化端点身份与模型配置行
版本——任何模型设置保存都会产生新版本号，旧授权随即失效，必须重新说明
（计划 §6.2）。发送边界（``ensure_send_authorized``）在每次实际发送与
结果落库前各核对一次，重试与 repair 同样受约束；核对只读库，gateway
由调用方在事务外构造（``build_analysis_gateway`` 强制关闭通用缓存）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from sqlalchemy.orm import Session

from papersteward.config import Settings, validate_llm_base_url
from papersteward.errors import InvalidRequestError, NotAuthorizedError, NotFoundError
from papersteward.llm.factory import build_llm_provider
from papersteward.llm.gateway import LLMGateway
from papersteward.llm.provider import LLMCompletion, LLMProvider
from papersteward.llm.runtime import CONFIG_KEY, LLMRuntimeConfig, resolve_llm_config
from papersteward.reading.insights.contracts import (
    ANALYSIS_CONSENT_VERSION,
    ANALYSIS_PURPOSE,
    SYNTHESIS_PURPOSE,
    AnalysisAuthorizationStatus,
    AnalysisGrantView,
    MaterialKind,
)
from papersteward.storage.insights import (
    ReadingAnalysisGrantRepository,
    ReadingAnalysisRevokeRepository,
)
from papersteward.storage.mappers import as_utc
from papersteward.storage.models import (
    ReadingAnalysisGrantRow,
    ReadingAnalysisRevokeEventRow,
)
from papersteward.storage.repositories import ConfigRepository

__all__ = [
    "ResolvedSendConfig",
    "active_grant_for_purpose",
    "analysis_authorization_status",
    "build_analysis_gateway",
    "ensure_send_authorized",
    "grant_analysis_authorization",
    "normalize_endpoint_identity",
    "revoke_analysis_authorization",
]


@dataclass(frozen=True)
class ResolvedSendConfig:
    """发送边界核对通过后的绑定快照；gateway 只能据此在事务外构造。"""

    grant_id: str
    purpose: str
    model_config_version: str
    runtime: LLMRuntimeConfig


def normalize_endpoint_identity(base_url: str) -> str:
    """规范化端点身份：与模型设置同一 base_url 校验规则（去尾斜杠等）。"""
    return validate_llm_base_url(base_url)


def grant_analysis_authorization(
    session: Session,
    *,
    scope_mode: str,
    material_classes: list[MaterialKind],
    settings: Settings,
    now: datetime,
    purpose: str = ANALYSIS_PURPOSE,
    manifest_hash: str | None = None,
) -> AnalysisGrantView:
    """新授权一条阅读分析用途；模型连接缺失或不可用时不接受授权。"""
    if purpose not in {ANALYSIS_PURPOSE, SYNTHESIS_PURPOSE, "reading_profile_inference"}:
        raise InvalidRequestError("阅读分析用途无效", details={"field": "purpose"})
    runtime = resolve_llm_config(session, settings)
    if (problem := runtime.readiness_problem()) is not None:
        raise InvalidRequestError(f"请先在设置页完成模型连接，再授权阅读分析（{problem}）")
    row = ConfigRepository(session).latest(CONFIG_KEY)
    if row is None:
        raise InvalidRequestError("尚未连接模型：请先在设置页保存模型连接")
    grant = ReadingAnalysisGrantRepository(session).add(
        ReadingAnalysisGrantRow(
            id=uuid4().hex,
            purpose=purpose,
            scope_mode=scope_mode,
            endpoint_identity=normalize_endpoint_identity(runtime.base_url or ""),
            model_config_version=row.version,
            model_name=runtime.model,
            material_classes_json=sorted(set(material_classes)),
            manifest_hash=manifest_hash,
            consent_version=ANALYSIS_CONSENT_VERSION,
            created_at=as_utc(now),
        )
    )
    return _grant_view(grant)


def revoke_analysis_authorization(
    session: Session, *, grant_id: str, now: datetime
) -> AnalysisGrantView:
    """撤回授权：置撤销时间并留审计（只存 ID 与原因）；重复撤回幂等。"""
    grant = ReadingAnalysisGrantRepository(session).get(grant_id)
    if grant is None:
        raise NotFoundError("授权不存在")
    if grant.revoked_at is None:
        grant.revoked_at = as_utc(now)
        ReadingAnalysisRevokeRepository(session).add(
            ReadingAnalysisRevokeEventRow(
                id=uuid4().hex,
                grant_id=grant.id,
                purpose=grant.purpose,
                reason="user",
                created_at=as_utc(now),
            )
        )
        from papersteward.reading.insights.memory import invalidate_memory_sources

        invalidate_memory_sources(session, grant_id=grant.id)
        session.flush()
    return _grant_view(grant)


def active_grant_for_purpose(
    session: Session, *, purpose: str, manifest_hash: str | None = None
) -> ReadingAnalysisGrantRow | None:
    """当前用途的活动授权：优先工作区持续授权，其次绑定该清单的本次授权。"""
    grants = ReadingAnalysisGrantRepository(session).active_for_purpose(purpose)
    grant = next((item for item in grants if item.scope_mode == "workspace"), None)
    if grant is not None or manifest_hash is None:
        return grant
    return next(
        (
            item
            for item in grants
            if item.scope_mode == "once" and item.manifest_hash == manifest_hash
        ),
        None,
    )


def ensure_send_authorized(
    session: Session,
    *,
    settings: Settings,
    purpose: str = ANALYSIS_PURPOSE,
    manifest_hash: str | None = None,
    material_kinds: tuple[str, ...] | list[str] = (),
) -> ResolvedSendConfig:
    """发送边界：无授权、配置已变、端点已变或类别超出范围一律 403 拒绝。

    每次实际发送前与结果落库前都应调用；本函数只读库，不做任何网络请求。
    """
    grant = active_grant_for_purpose(session, purpose=purpose, manifest_hash=manifest_hash)
    if grant is None:
        raise NotAuthorizedError(
            "阅读分析尚未授权：分析前请先在阅读分析设置中核对材料与数据去向并授权"
        )
    row = ConfigRepository(session).latest(CONFIG_KEY)
    if row is None or row.version != grant.model_config_version:
        raise NotAuthorizedError("模型连接或配置已变化，原授权失效；请重新核对数据去向并再次授权")
    runtime = resolve_llm_config(session, settings)
    if (problem := runtime.readiness_problem()) is not None:
        raise NotAuthorizedError(f"模型连接当前不可用，已停止发送（{problem}）")
    if normalize_endpoint_identity(runtime.base_url or "") != grant.endpoint_identity:
        raise NotAuthorizedError("模型端点已变化，原授权不能沿用；请重新核对并再次授权")
    classes = set(grant.material_classes_json or [])
    missing = sorted(set(material_kinds) - classes)
    if missing:
        raise NotAuthorizedError(
            "材料类别超出授权范围，请重新授权扩大类别或从清单中排除",
            details={"missing_classes": missing},
        )
    return ResolvedSendConfig(
        grant_id=grant.id,
        purpose=grant.purpose,
        model_config_version=grant.model_config_version,
        runtime=runtime,
    )


def build_analysis_gateway(
    resolved: ResolvedSendConfig,
    *,
    transport: object | None = None,
    before_send: Callable[[], None] | None = None,
) -> LLMGateway:
    """分析发送专用的 gateway 构造；通用缓存一律关闭（计划 §6.3）。

    通用 ``DatabaseLLMCache`` 的键只有 model+prompt+input_hash，缺少端点与
    授权绑定，不能用于私人分析；R2-03 起的分析 runner 只能经本函数构造。
    """
    provider = build_llm_provider(resolved.runtime, live=True, transport=transport)  # type: ignore[arg-type]
    return LLMGateway(
        _GuardedProvider(provider, before_send) if before_send is not None else provider,
        max_attempts=resolved.runtime.max_attempts,
        timeout_s=resolved.runtime.timeout_s,
        cache=None,
    )


def analysis_authorization_status(
    session: Session, *, settings: Settings, purpose: str = ANALYSIS_PURPOSE
) -> AnalysisAuthorizationStatus:
    """授权现状视图：模型连接摘要与授权列表分开表达，不触发任何发送。"""
    runtime = resolve_llm_config(session, settings)
    configured = runtime.source == "db" and runtime.readiness_problem() is None
    row = ConfigRepository(session).latest(CONFIG_KEY)
    return AnalysisAuthorizationStatus(
        llm_configured=configured,
        llm_endpoint_identity=(
            normalize_endpoint_identity(runtime.base_url or "") if runtime.base_url else None
        ),
        llm_model=runtime.model,
        llm_config_version=row.version if row is not None else None,
        grants=[
            _grant_view(item)
            for item in ReadingAnalysisGrantRepository(session).list_all()
            if item.purpose == purpose
        ],
    )


def _grant_view(grant: ReadingAnalysisGrantRow) -> AnalysisGrantView:
    return AnalysisGrantView(
        grant_id=grant.id,
        purpose=grant.purpose,
        scope_mode=grant.scope_mode,
        endpoint_identity=grant.endpoint_identity,
        model_config_version=grant.model_config_version,
        model_name=grant.model_name,
        material_classes=list(grant.material_classes_json or []),
        manifest_hash=grant.manifest_hash,
        consent_version=grant.consent_version,
        created_at=as_utc(grant.created_at),
        revoked_at=as_utc(grant.revoked_at) if grant.revoked_at is not None else None,
    )


class _GuardedProvider:
    """每次真实调用（包括 retry/repair）都重新核对授权、取消和材料版本。"""

    def __init__(self, provider: LLMProvider, guard: Callable[[], None]):
        self.provider = provider
        self.guard = guard

    @property
    def model_id(self) -> str:
        return self.provider.model_id

    def complete(self, prompt: str, *, timeout_s: float) -> LLMCompletion:
        self.guard()
        return self.provider.complete(prompt, timeout_s=timeout_s)

    def close(self) -> None:
        close = getattr(self.provider, "close", None)
        if close is not None:
            close()
