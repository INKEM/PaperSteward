"""研究记忆仓储：身份按证据组唯一，版本只追加。"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from papersteward.storage.models import ReadingMemoryRevisionRow, ReadingMemoryRow


class ReadingMemoryRepository:
    def __init__(self, session: Session):
        self.session = session

    def by_group(self, key: str) -> ReadingMemoryRow | None:
        return self.session.scalar(
            select(ReadingMemoryRow).where(ReadingMemoryRow.group_key == key)
        )

    def latest(self, memory_id: str) -> ReadingMemoryRevisionRow | None:
        return self.session.scalar(
            select(ReadingMemoryRevisionRow)
            .where(ReadingMemoryRevisionRow.memory_id == memory_id)
            .order_by(ReadingMemoryRevisionRow.revision.desc())
            .limit(1)
        )

    def current(self) -> list[ReadingMemoryRevisionRow]:
        latest = (
            select(
                ReadingMemoryRevisionRow.memory_id,
                func.max(ReadingMemoryRevisionRow.revision).label("revision"),
            )
            .group_by(ReadingMemoryRevisionRow.memory_id)
            .subquery()
        )
        return list(
            self.session.scalars(
                select(ReadingMemoryRevisionRow)
                .join(
                    latest,
                    (ReadingMemoryRevisionRow.memory_id == latest.c.memory_id)
                    & (ReadingMemoryRevisionRow.revision == latest.c.revision),
                )
                .order_by(
                    ReadingMemoryRevisionRow.recorded_at.desc(), ReadingMemoryRevisionRow.memory_id
                )
            )
        )

    def revisions(self, memory_id: str) -> list[ReadingMemoryRevisionRow]:
        return list(
            self.session.scalars(
                select(ReadingMemoryRevisionRow)
                .where(ReadingMemoryRevisionRow.memory_id == memory_id)
                .order_by(ReadingMemoryRevisionRow.revision)
            )
        )

    def request(self, key: str) -> ReadingMemoryRevisionRow | None:
        return self.session.scalar(
            select(ReadingMemoryRevisionRow).where(ReadingMemoryRevisionRow.request_key == key)
        )
