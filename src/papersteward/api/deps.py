"""FastAPI 依赖：按请求提供数据库 Session，不做长生命周期持有。"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session, sessionmaker


def get_session(request: Request) -> Iterator[Session]:
    """从 app.state 取 Session 工厂，关闭交给依赖清理。

    P0 的接口是只读的，因此这里不提交；后续写接口应使用显式事务。
    """
    factory: sessionmaker[Session] = request.app.state.session_factory
    session = factory()
    try:
        yield session
    finally:
        session.close()


SessionDep = Annotated[Session, Depends(get_session)]

__all__ = ["SessionDep", "get_session"]
