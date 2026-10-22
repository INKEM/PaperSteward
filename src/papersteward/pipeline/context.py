"""只消费初始化已确认的精确版本；复核前序证据，拒绝漂移。"""

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from papersteward.core.entities import ResearchProfile
from papersteward.core.enums import DocumentStatus, EventType
from papersteward.core.errors import InvariantViolation
from papersteward.onboarding.dry_run import RuntimeParameters
from papersteward.onboarding.dry_run_service import read_preview
from papersteward.scoring.config import ScoringConfig, validate_scoring_config
from papersteward.sources.queries import selected_profile
from papersteward.storage.models import EventRow
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
    events: dict[EventType, EventRow] = {}
    for kind in (EventType.ONBOARDING_G2_CONFIRMED, EventType.ONBOARDING_G4_CONFIRMED):
        event = session.scalar(
            select(EventRow)
            .where(EventRow.event_type == kind.value)
            .order_by(EventRow.created_at.desc(), EventRow.id.desc())
            .limit(1)
        )
        if event is None:
            raise InvariantViolation("onboarding.binding", "缺少确认事件")
        events[kind] = event
    g2 = events[EventType.ONBOARDING_G2_CONFIRMED].payload_json
    g4 = events[EventType.ONBOARDING_G4_CONFIRMED]
    try:
        evidence = g4.payload_json["evidence"]
        view = read_preview(session, evidence["version"])
        taxonomy = TaxonomyRepository(session).get(g2["artifact_id"], g2["artifact_version"])
        runtime_config = ConfigRepository(session).get(
            "onboarding_runtime", evidence["runtime_version"]
        )
        g4_scoring = ConfigRepository(session).get("scoring", evidence["scoring_version"])
        if (
            taxonomy is None
            or taxonomy.status is not DocumentStatus.CONFIRMED
            or runtime_config is None
            or g4_scoring is None
            or view["report"] != evidence["report"]
            or view["report_hash"] != evidence["report_hash"]
            or view["feedback_hash"] != evidence["feedback_hash"]
            or dict(runtime_config.value_json) != view["report"]["parameters"]
            or dict(g4_scoring.value_json) != view["report"]["scoring"]
        ):
            raise InvariantViolation("onboarding.binding", "选定版本或确认依据已漂移")
        runtime = RuntimeParameters.model_validate(runtime_config.value_json)
        target_day = day or now.astimezone(ZoneInfo(runtime.timezone)).date()
        activation = ScoringChangeRepository(session).active_for(target_day)
        scoring_version = (
            activation.config_version if activation is not None else g4_scoring.version
        )
        scoring_config = ConfigRepository(session).get("scoring", scoring_version)
        if scoring_config is None:
            raise InvariantViolation(
                "onboarding.binding", f"已激活的评分配置版本 {scoring_version} 不存在"
            )
        validate_scoring_config(ScoringConfig.model_validate(scoring_config.value_json))
        clock = now.astimezone(ZoneInfo(runtime.timezone)).strftime("%H:%M")
        start, end = runtime.quiet_start, runtime.quiet_end
        quiet = bool(
            start
            and end
            and (start <= clock < end if start < end else clock >= start or clock < end)
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
            "g4_event_id": g4.id,
            "runtime_version": runtime_config.version,
            "scoring_version": scoring_version,
            "base_scoring_version": g4_scoring.version,
            "scoring_change_id": activation.id if activation is not None else None,
            "report_hash": view["report_hash"],
        }
    except (KeyError, TypeError) as exc:
        raise InvariantViolation("onboarding.binding", "确认事件缺少完整绑定") from exc


def runtime_local_day(session: Session, now: datetime, binding: dict[str, Any]) -> date:
    """按已绑定 runtime timezone 取本地日期；不把动态日期放进 binding hash。"""
    runtime_config = ConfigRepository(session).get("onboarding_runtime", binding["runtime_version"])
    if runtime_config is None:
        raise InvariantViolation("onboarding.binding", "绑定运行参数不存在")
    runtime = RuntimeParameters.model_validate(runtime_config.value_json)
    return now.astimezone(ZoneInfo(runtime.timezone)).date()
