"""SQLite 支撑的 LLM 结果缓存。

每次 get/put 都用独立短会话，网关跨线程共享缓存实例时不会复用连接；
缓存自身的任何故障（写锁、磁盘）都被吞掉并按未命中处理，绝不阻塞挖掘主流程。
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import sessionmaker

from papersteward.core.entities import utc_now
from papersteward.llm.gateway import LLMCache, LLMCacheEntry
from papersteward.storage.db import session_scope
from papersteward.storage.models import LLMCacheRow

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


class DatabaseLLMCache(LLMCache):
    """llm_cache 表的读写实现；命中返回条目，任何异常按未命中/丢弃处理。"""

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory

    def get(self, *, model: str, prompt_version: str, input_hash: str) -> LLMCacheEntry | None:
        try:
            with session_scope(self._factory) as session:
                row = session.scalar(
                    select(LLMCacheRow).where(
                        LLMCacheRow.model == model,
                        LLMCacheRow.prompt_version == prompt_version,
                        LLMCacheRow.input_hash == input_hash,
                    )
                )
                if row is None:
                    return None
                return LLMCacheEntry(
                    raw_text=row.raw_text,
                    repair_used=bool(row.repair_used),
                    prompt_version=row.prompt_version,
                    generated_at=row.generated_at,
                )
        except Exception:
            return None

    def put(
        self,
        entry: LLMCacheEntry,
        *,
        model: str,
        prompt_version: str,
        input_hash: str,
    ) -> None:
        try:
            with session_scope(self._factory) as session:
                if session.get_bind().dialect.name == "sqlite":
                    # 唯一索引兜底并发写入：一条 INSERT OR IGNORE 即可，省存在性预查。
                    statement = sqlite_insert(LLMCacheRow).values(
                        id=f"llmcache-{uuid4().hex[:24]}",
                        model=model,
                        prompt_version=prompt_version,
                        input_hash=input_hash,
                        raw_text=entry.raw_text,
                        repair_used=entry.repair_used,
                        generated_at=entry.generated_at,
                        created_at=utc_now(),
                    )
                    session.execute(statement.on_conflict_do_nothing())
                    return
                existing = session.scalar(
                    select(LLMCacheRow.id).where(
                        LLMCacheRow.model == model,
                        LLMCacheRow.prompt_version == prompt_version,
                        LLMCacheRow.input_hash == input_hash,
                    )
                )
                if existing is not None:
                    return
                session.add(
                    LLMCacheRow(
                        id=f"llmcache-{uuid4().hex[:24]}",
                        model=model,
                        prompt_version=prompt_version,
                        input_hash=input_hash,
                        raw_text=entry.raw_text,
                        repair_used=entry.repair_used,
                        generated_at=entry.generated_at,
                        created_at=utc_now(),
                    )
                )
        except Exception:
            return


__all__ = ["DatabaseLLMCache"]
