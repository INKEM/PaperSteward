"""只消费已确认的研究档案、分类法与运行参数；每次运行时重新解析绑定。"""

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from papersteward.core.entities import ResearchProfile
from papersteward.core.errors import InvariantViolation
from papersteward.onboarding.parameters import RuntimeParameters
from papersteward.onboarding.setup import RUNTIME_KEY
from papersteward.scoring.config import ScoringConfig, validate_scoring_config
from papersteward.sources.queries import selected_profile
from papersteward.storage.onboarding import ensure_active
from papersteward.storage.repositories import (
    ConfigRepository,
    ScoringChangeRepository,
    TaxonomyRepository,
)


def bound_context(
    session: Session,
    now: datetime,
    *,
    action: str = "ingest",
    enforce_schedule: bool = True,
    day: date | None = None,
) -> tuple[ResearchProfile, dict[str, Any]]:
    ensure_active(session, action=action)
    if now.tzinfo is None or now.utcoffset() is None:
        raise InvariantViolation("ingest.time", "运行时间必须带时区")
    profile = selected_profile(session)
    taxonomy = TaxonomyRepository(session).latest_confirmed()
    if taxonomy is None:
        raise InvariantViolation("onboarding.binding", "缺少已确认的分类法，请先完成初始化确认")
    runtime_config = ConfigRepository(session).latest(RUNTIME_KEY)
    if runtime_config is None:
        raise InvariantViolation("onboarding.binding", "缺少运行参数，请重新完成初始化确认")
    runtime = RuntimeParameters.model_validate(dict(runtime_config.value_json))
    target_day = day or now.astimezone(ZoneInfo(runtime.timezone)).date()

    changes = ScoringChangeRepository(session)
    activation = changes.active_for(target_day)
    configs = ConfigRepository(session)
    scoring_config = None
    base_scoring_version = None
    if activation is not None:
        scoring_config = configs.get("scoring", activation.config_version)
        base_scoring_version = activation.base_version
    else:
        # 已确认但尚未生效的变更会提前写入新配置；生效前仍读其 base_version。
        pending = changes.pending_after(target_day)
        if pending:
            scoring_config = configs.get("scoring", pending[0].base_version)
            base_scoring_version = scoring_config.version if scoring_config else None
    if scoring_config is None:
        scoring_config = configs.latest("scoring")
        base_scoring_version = scoring_config.version if scoring_config else None
    if scoring_config is None:
        raise InvariantViolation("onboarding.binding", "缺少评分配置，请重新完成初始化确认")
    validate_scoring_config(ScoringConfig.model_validate(dict(scoring_config.value_json)))

    clock = now.astimezone(ZoneInfo(runtime.timezone)).strftime("%H:%M")
    start, end = runtime.quiet_start, runtime.quiet_end
    quiet = bool(
        start and end and (start <= clock < end if start < end else clock >= start or clock < end)
    )
    if enforce_schedule and (runtime.paused or quiet):
        raise InvariantViolation(
            "ingest.schedule", "当前处于暂停或安静时段，未执行处理；请稍后重试"
        )
    return profile, {
        "profile_id": profile.id,
        "profile_version": profile.version,
        "taxonomy_id": taxonomy.id,
        "taxonomy_version": taxonomy.version,
        "runtime_version": runtime_config.version,
        "scoring_version": scoring_config.version,
        "base_scoring_version": base_scoring_version or scoring_config.version,
        "scoring_change_id": activation.id if activation is not None else None,
    }


def runtime_local_day(session: Session, now: datetime, binding: dict[str, Any]) -> date:
    """按已绑定 runtime timezone 取本地日期；不把动态日期放进 binding hash。"""
    config = ConfigRepository(session)
    runtime_config = config.get(RUNTIME_KEY, binding["runtime_version"])
    if runtime_config is None:
        runtime_config = config.latest(RUNTIME_KEY)
    if runtime_config is None:
        raise InvariantViolation("onboarding.binding", "绑定运行参数不存在")
    runtime = RuntimeParameters.model_validate(dict(runtime_config.value_json))
    return now.astimezone(ZoneInfo(runtime.timezone)).date()
