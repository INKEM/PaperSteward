"""确认草案并激活工作区：档案 + 分类法 + 默认运行参数一次确认。"""

from datetime import datetime
from importlib.resources import files
from uuid import uuid4

import yaml
from sqlalchemy.orm import Session

from papersteward.core.entities import Config, Event
from papersteward.core.enums import AssignedBy, DocumentStatus, EventType
from papersteward.core.errors import DomainValidationError, InvariantViolation
from papersteward.core.onboarding import Onboarding, OnboardingStage
from papersteward.documents.service import confirm_profile, confirm_taxonomy
from papersteward.onboarding.parameters import default_runtime_parameters
from papersteward.onboarding.profile import content_hash as profile_content_hash
from papersteward.onboarding.profile import load_profile, missing_fields, parse_draft
from papersteward.onboarding.service import transition_active
from papersteward.onboarding.taxonomy import content_hash as taxonomy_content_hash
from papersteward.onboarding.taxonomy import load_taxonomy, parse_taxonomy_draft
from papersteward.scoring.config import ScoringConfig
from papersteward.scoring.service import CONFIG_KEY as SCORING_CONFIG_KEY
from papersteward.scoring.service import save_scoring_config
from papersteward.storage.onboarding import get_onboarding
from papersteward.storage.repositories import ConfigRepository, EventRepository

RUNTIME_KEY = "onboarding_runtime"

STARTER_SCORING_VERSION = "1.0.0"


def default_scoring_config() -> ScoringConfig:
    """内置 starter scoring.yaml 作为默认评分配置；治理流程可后续调整。"""
    resource = files("papersteward.resources").joinpath("starter", "scoring.yaml")
    text = resource.read_text(encoding="utf-8")
    return ScoringConfig.model_validate(yaml.safe_load(text))


def confirm_setup(
    session: Session,
    *,
    confirmed_by: str,
    change_summary: str,
    expected_revision: int,
    profile_id: str,
    profile_version: str,
    profile_hash: str,
    taxonomy_id: str,
    taxonomy_version: str,
    taxonomy_hash: str,
    timezone: str,
    event_id: str,
    occurred_at: datetime,
) -> Onboarding:
    """校验两份草案后确认、写入运行参数并激活；任一步失败整体回滚。"""
    state = get_onboarding(session)
    if state.stage is not OnboardingStage.DRAFT:
        raise InvariantViolation(
            "onboarding.stage", f"当前 {state.stage.value} 不允许确认；请先生成研究草案"
        )

    profile = load_profile(session, profile_id, profile_version)
    if profile.status is DocumentStatus.ARCHIVED:
        raise InvariantViolation("profile.status", "已归档档案不能确认")
    parsed = parse_draft(profile.content_md, profile.id)
    if parsed.version != profile.version or dict(parsed.parsed_json) != dict(profile.parsed_json):
        raise InvariantViolation("profile.parsed_json", "档案缓存与正文不一致，请重新生成草案")
    if profile_content_hash(profile.content_md) != profile_hash:
        raise InvariantViolation("profile.content_hash", "档案内容已变化，请重新审阅后确认")
    if missing := missing_fields(parsed):
        raise DomainValidationError("profile.content_md", f"档案不完整，请补充：{missing[0]}")

    taxonomy = load_taxonomy(session, taxonomy_id, taxonomy_version)
    if taxonomy.status is DocumentStatus.ARCHIVED:
        raise InvariantViolation("taxonomy.status", "已归档分类法不能确认")
    parsed_taxonomy = parse_taxonomy_draft(taxonomy.content_md, taxonomy.id)
    if parsed_taxonomy.version != taxonomy.version:
        raise InvariantViolation("taxonomy.content", "分类法正文与缓存不一致，请重新生成草案")
    if taxonomy_content_hash(taxonomy.content_md) != taxonomy_hash:
        raise InvariantViolation("taxonomy.content_hash", "分类法已变化，请重新审阅后确认")

    codes = sorted(
        tag.code for tag in parsed_taxonomy.tags if tag.level.value == "primary" and tag.active
    )
    shares = {code: round(1.0 / len(codes), 6) for code in codes}
    drift = round(1.0 - sum(shares.values()), 6)
    if shares:
        shares[codes[-1]] = round(shares[codes[-1]] + drift, 6)
    runtime = default_runtime_parameters(timezone=timezone, target_shares=shares)

    confirm_profile(session, profile.id, profile.version, confirmed_at=occurred_at)
    confirm_taxonomy(session, taxonomy.id, taxonomy.version, confirmed_at=occurred_at)

    repository = ConfigRepository(session)
    scoring_row = repository.latest(SCORING_CONFIG_KEY)
    if scoring_row is None:
        scoring_row = save_scoring_config(
            session,
            default_scoring_config(),
            updated_by=AssignedBy.SYSTEM,
            updated_at=occurred_at,
        )
    scoring_version = scoring_row.version
    runtime_version = f"v-{uuid4().hex[:16]}"
    repository.add(
        Config(
            key=RUNTIME_KEY,
            version=runtime_version,
            value_json=runtime.model_dump(mode="json", exclude_unset=True),
            updated_by=AssignedBy.USER,
            updated_at=occurred_at,
        )
    )

    updated = transition_active(
        session, expected_revision=expected_revision, occurred_at=occurred_at
    )
    EventRepository(session).append(
        Event(
            id=event_id,
            event_type=EventType.ONBOARDING_CONFIRMED,
            created_at=occurred_at,
            payload_json={
                "confirmed_by": confirmed_by,
                "change_summary": change_summary,
                "profile": {"id": profile.id, "version": profile.version},
                "taxonomy": {"id": taxonomy.id, "version": taxonomy.version},
                "runtime_version": runtime_version,
                "scoring_version": scoring_version,
                "revision": updated.revision,
            },
        )
    )
    return updated


__all__ = ["RUNTIME_KEY", "confirm_setup", "default_scoring_config"]
