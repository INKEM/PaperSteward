"""用户显式选择的个人完整副本；只读，不创建报告。"""

from datetime import datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from papersteward.reading.insights.assets import _revision_view
from papersteward.reading.insights.contracts import InsightPeriodRequest, InsightQueryRequest
from papersteward.reading.insights.projection import BATCH_SIZE, build_projection
from papersteward.reading.insights.timeline import workspace_timezone
from papersteward.storage.insights import ReadingAssetRepository, ReadingTimeRevisionRepository
from papersteward.storage.mappers import as_utc
from papersteward.storage.models import ReadingAssetRow


def personal_copy(session: Session, *, now: datetime) -> dict[str, Any]:
    query = InsightQueryRequest(
        period=InsightPeriodRequest(kind="all", timezone=workspace_timezone(session).key)
    )
    session.execute(text("BEGIN DEFERRED"))
    try:
        projection = build_projection(session, query, collected_at=now, shared_snapshot=True)
        dates = {}
        paper_ids = [paper.paper_id for paper in projection.papers]
        for offset in range(0, len(paper_ids), BATCH_SIZE):
            dates.update(
                ReadingTimeRevisionRepository(session).list_for_papers(
                    paper_ids[offset : offset + BATCH_SIZE]
                )
            )
        repository = ReadingAssetRepository(session)
        assets = [
            {
                "asset_id": asset.id,
                "kind": asset.kind,
                "created_at": as_utc(asset.created_at),
                "revisions": [
                    _revision_view(repository, row) for row in repository.revisions(asset.id)
                ],
            }
            for asset in session.scalars(select(ReadingAssetRow).order_by(ReadingAssetRow.id))
        ]
        from papersteward.reading.insights.memory import memory_revision_view
        from papersteward.storage.reading_memory import ReadingMemoryRepository

        memory_repository = ReadingMemoryRepository(session)
        memory = [
            {
                "memory_id": current.memory_id,
                "revisions": [
                    memory_revision_view(session, revision)
                    for revision in memory_repository.revisions(current.memory_id)
                ],
            }
            for current in memory_repository.current()
        ]
        return {
            "research_memory": memory,
            "contract": "reading-insights.personal-copy/1",
            "exported_at": now,
            "timezone": query.period.timezone,
            "papers": projection.papers,
            "reading_time_revisions": dates,
            "assets": assets,
        }
    finally:
        session.rollback()
