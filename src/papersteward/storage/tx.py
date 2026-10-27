"""通用 SQLite 事务工具；失败操作自身原子回滚，提交由外层事务决定。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text
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


@contextmanager
def write_tx(session: Session) -> Iterator[None]:
    """请求级短写事务：SQLite 上以 BEGIN IMMEDIATE 开写，提交或整体回滚。

    回执查询、回执行插入、版本检查与业务写入必须在同一把写锁内完成；
    延迟 BEGIN 的"读后写"升级会撞 BUSY_SNAPSHOT 立即失败，写接口一律用本工具。
    """
    if session.get_bind().dialect.name == "sqlite":
        session.execute(text("BEGIN IMMEDIATE"))
    try:
        yield
        session.commit()
    except Exception:
        session.rollback()
        raise


__all__ = ["atomic", "write_tx"]
