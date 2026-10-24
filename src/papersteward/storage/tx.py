"""通用 SQLite SAVEPOINT 工具；失败操作自身原子回滚，提交由外层事务决定。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.orm import Session


@contextmanager
def atomic(session: Session) -> Iterator[None]:
    # sqlite3 legacy 模式不会为 SELECT 开启事务。裸 SAVEPOINT 被 release 后可能
    # 提前提交，导致调用方 rollback 无效；先确保实际存在外层事务。
    connection = session.connection()
    raw = connection.connection.dbapi_connection
    if isinstance(raw, sqlite3.Connection) and not raw.in_transaction:
        connection.exec_driver_sql("BEGIN")
    with session.begin_nested():
        yield


__all__ = ["atomic"]
