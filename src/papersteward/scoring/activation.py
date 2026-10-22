"""已确认但尚未生效的权重变更：为新处理/提升文献补写生效日静态快照。"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import date, datetime

from sqlalchemy.orm import Session

from papersteward.core.entities import StaticScoreSnapshot
from papersteward.core.errors import InvariantViolation
from papersteward.scoring.config import ScoringConfig, validate_scoring_config
from papersteward.storage.repositories import (
    ConfigRepository,
    ScoringChangeRepository,
    StaticScoreSnapshotRepository,
)


def append_future_activation_snapshots(
    session: Session,
    *,
    paper_id: str,
    relevance: float,
    authority: float,
    extra_value: float,
    confidence: float,
    profile_version: str,
    taxonomy_version: str,
    base_input_hash: str,
    breakdown: Mapping[str, object],
    after_day: date,
    now: datetime,
) -> list[StaticScoreSnapshot]:
    """为 ``after_day`` 之后每个已确认 activation 追加对应生效日静态快照。

    确认时只覆盖当时已存在的快照；确认后到生效日之间新处理/提升的文献必须在这里
    补写，否则生效日队列会因 ``static_version_mismatch`` 丢失它们。只做确定性
    R/A/E 线性重算，不重新调用 LLM，也不改写历史快照。
    """
    changes = ScoringChangeRepository(session).pending_after(after_day)
    if not changes:
        return []
    repository = StaticScoreSnapshotRepository(session)
    latest = repository.latest(paper_id)
    created: list[StaticScoreSnapshot] = []
    for change in changes:
        if latest is not None and latest.score_date >= change.effective_on:
            continue
        row = ConfigRepository(session).get("scoring", change.config_version)
        if row is None:
            raise InvariantViolation(
                "config.activation", f"激活配置版本 {change.config_version} 不存在"
            )
        config = validate_scoring_config(ScoringConfig.model_validate(row.value_json))
        static_score = round(
            relevance * config.static.relevance
            + authority * config.static.authority
            + extra_value * config.static.extra_value,
            1,
        )
        payload = dict(breakdown)
        payload["activation_pending"] = {
            "change_id": change.id,
            "effective_on": change.effective_on.isoformat(),
            "config_version": config.version,
        }
        snapshot = StaticScoreSnapshot(
            paper_id=paper_id,
            score_date=change.effective_on,
            relevance=relevance,
            authority=authority,
            extra_value=extra_value,
            static_score=static_score,
            confidence=confidence,
            config_version=config.version,
            profile_version=profile_version,
            taxonomy_version=taxonomy_version,
            input_hash=hashlib.sha256(
                f"{base_input_hash}:activation:{change.id}:"
                f"{change.effective_on.isoformat()}".encode()
            ).hexdigest(),
            breakdown_json=payload,
            created_at=now,
        )
        repository.append(snapshot)
        latest = snapshot
        created.append(snapshot)
    return created


__all__ = ["append_future_activation_snapshots"]
