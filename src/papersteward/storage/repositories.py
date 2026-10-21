"""P0 仓储：只做会话内的最小 CRUD，不自行提交事务。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from papersteward.storage.db import utc_now
from papersteward.storage.models import Paper


class PaperRepository:
    """文献仓储；事务边界由调用方（API/CLI/任务）控制。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        paper_id: str,
        title: str,
        *,
        status: str = "new",
        created_at: datetime | None = None,
    ) -> Paper:
        paper = Paper(
            id=paper_id,
            title=title,
            status=status,
            created_at=created_at or utc_now(),
        )
        self._session.add(paper)
        self._session.flush()
        return paper

    def get(self, paper_id: str) -> Paper | None:
        return self._session.get(Paper, paper_id)

    def list(self, *, limit: int | None = None, offset: int = 0) -> list[Paper]:
        statement = select(Paper).order_by(Paper.created_at, Paper.id).offset(offset)
        if limit is not None:
            statement = statement.limit(limit)
        return list(self._session.scalars(statement).all())

    def count(self) -> int:
        return self._session.scalar(select(func.count()).select_from(Paper)) or 0

    def update_status(self, paper_id: str, status: str) -> Paper | None:
        paper = self.get(paper_id)
        if paper is None:
            return None
        paper.status = status
        self._session.flush()
        return paper

    def delete(self, paper_id: str) -> bool:
        paper = self.get(paper_id)
        if paper is None:
            return False
        self._session.delete(paper)
        self._session.flush()
        return True


__all__ = ["PaperRepository"]
