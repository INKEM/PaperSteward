"""处理管线的运行上下文组装；供 CLI 与挖掘任务共用（不得反向依赖 CLI 层）。"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy.orm import Session

from papersteward.core.entities import Bucket, ResearchProfile, Taxonomy
from papersteward.errors import InvalidRequestError
from papersteward.pipeline.context import bound_context, runtime_local_day
from papersteward.pipeline.process import ensure_topic_buckets
from papersteward.scoring.config import ScoringConfig, validate_scoring_config
from papersteward.storage.repositories import ConfigRepository, TaxonomyRepository


def processor_context(
    session: Session, now: datetime, *, ensure_buckets: bool = True
) -> tuple[ResearchProfile, Taxonomy, ScoringConfig, dict[str, Bucket], date]:
    """处理管线的运行上下文；``ensure_buckets=False`` 时完全不写库。

    并行挖掘 worker 的只读阶段用 False 取上下文，主题桶落库留给写阶段。
    """
    profile, binding = bound_context(session, now, action="process")
    taxonomy = TaxonomyRepository(session).get(binding["taxonomy_id"], binding["taxonomy_version"])
    if taxonomy is None:
        raise InvalidRequestError("选定分类法不存在")
    config_row = ConfigRepository(session).get("scoring", binding["scoring_version"])
    if config_row is None:
        raise InvalidRequestError("选定评分配置不存在")
    config = validate_scoring_config(ScoringConfig.model_validate(config_row.value_json))
    local_day = runtime_local_day(session, now, binding)
    buckets = ensure_topic_buckets(session, taxonomy, persist=ensure_buckets)
    return profile, taxonomy, config, buckets, local_day


__all__ = ["processor_context"]
