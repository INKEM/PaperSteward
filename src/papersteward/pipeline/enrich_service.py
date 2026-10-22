"""P4-S2 富化持久化服务：中性值、幂等 upsert 与可审计来源；不调用网络/LLM。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sqlalchemy.orm import Session

from papersteward.core.entities import Enrichment, Paper, utc_now
from papersteward.pipeline.enrich import EnrichmentProvider, NeutralEnricher
from papersteward.storage.repositories import EnrichmentRepository


def persist_enrichment(
    session: Session,
    paper: Paper,
    *,
    provider: EnrichmentProvider | None = None,
    now: Callable[[], datetime] = utc_now,
) -> tuple[Enrichment, bool]:
    """执行 provider 并保存；同一内容重复调用不改变 updated_at。"""
    if not isinstance(paper, Paper):
        raise TypeError("paper 必须是 Paper")
    active_provider = NeutralEnricher() if provider is None else provider
    at = now()
    enrichment = Enrichment(
        paper_id=paper.id,
        signals=active_provider.enrich(paper),
        created_at=at,
        updated_at=at,
    )
    return EnrichmentRepository(session).save(enrichment)


__all__ = ["persist_enrichment"]
