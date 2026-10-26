"""页面与 JSON 共享有效范围及同一真实只读快照。"""

from datetime import datetime

from sqlalchemy.orm import Session

from papersteward.reading.insights.contracts import InsightOverview, InsightQueryRequest
from papersteward.reading.insights.scope import collect_scope


def collect_overview(
    session: Session, query: InsightQueryRequest, *, collected_at: datetime | None = None
) -> InsightOverview:
    return collect_scope(session, query, collected_at=collected_at).overview
