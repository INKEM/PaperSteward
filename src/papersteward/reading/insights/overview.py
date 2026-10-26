"""一次一致快照中的事实概览，JSON、页面及导出共用。"""

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from papersteward.core.entities import utc_now
from papersteward.reading.insights.aggregate import build_overview
from papersteward.reading.insights.assets import asset_events
from papersteward.reading.insights.contracts import InsightOverview, InsightQueryRequest
from papersteward.reading.insights.projection import build_projection


def collect_overview(
    session: Session, query: InsightQueryRequest, *, collected_at: datetime | None = None
) -> InsightOverview:
    collected = collected_at or utc_now()
    session.execute(text("BEGIN DEFERRED"))
    try:
        projection = build_projection(session, query, collected_at=collected, shared_snapshot=True)
        events = asset_events(session, until=projection.knowledge_cutoff)
    finally:
        session.rollback()
    return build_overview(projection, tag=query.tag, asset_events=events)
