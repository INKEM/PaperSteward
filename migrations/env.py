"""Alembic 环境：数据库 URL 与元数据来自 PaperSteward 自身配置。"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection

from papersteward.config import get_settings
from papersteward.storage import Base, create_db_engine

config = context.config
target_metadata = Base.metadata

# 程序化迁移（测试/部署脚本）会传入现成连接；此时不要改动全局日志配置。
if config.config_file_name is not None and config.attributes.get("connection") is None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def _run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不建立数据库连接。"""
    context.configure(
        url=get_settings().db_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：优先使用调用方传入的连接，否则从 Settings 创建 Engine。"""
    connection = config.attributes.get("connection")
    if connection is not None:
        _run_migrations(connection)
        return

    engine = create_db_engine()
    try:
        with engine.connect() as own_connection:
            _run_migrations(own_connection)
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
