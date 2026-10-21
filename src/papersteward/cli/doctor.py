"""`papersteward doctor` 的检查逻辑。"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy.engine import Engine

from papersteward.config import Settings, format_settings_error, get_settings
from papersteward.storage import check_database, create_db_engine
from papersteward.storage.migrations import MigrationStatus, get_migration_status

CHECK_CONFIG = "配置"
CHECK_DATABASE = "数据库连接"
CHECK_MIGRATION = "迁移版本"


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class DoctorReport:
    checks: tuple[DoctorCheck, ...]

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)


def _migration_check(status: MigrationStatus) -> DoctorCheck:
    if status.head is None:
        return DoctorCheck(CHECK_MIGRATION, False, "未找到迁移脚本")
    if status.is_current:
        return DoctorCheck(
            CHECK_MIGRATION,
            True,
            f"current={status.current} head={status.head}",
        )
    return DoctorCheck(
        CHECK_MIGRATION,
        False,
        f"current={status.current or 'none'} head={status.head}；请运行 alembic upgrade head",
    )


def run_doctor(
    settings: Settings | None = None,
    *,
    engine: Engine | None = None,
) -> DoctorReport:
    """检查配置、数据库连接与迁移版本；不自动执行迁移。"""
    try:
        resolved_settings = settings or get_settings()
    except ValidationError as exc:
        return DoctorReport((DoctorCheck(CHECK_CONFIG, False, format_settings_error(exc)),))

    checks = [
        DoctorCheck(
            CHECK_CONFIG,
            True,
            f"env={resolved_settings.app_env.value}, data_dir={resolved_settings.data_dir}",
        )
    ]
    owns_engine = engine is None
    resolved_engine = engine
    if resolved_engine is None:
        try:
            resolved_engine = create_db_engine()
        except Exception:
            checks.append(
                DoctorCheck(
                    CHECK_DATABASE,
                    False,
                    "无法创建数据库引擎；请检查 PAPERSTEWARD_DATABASE_URL / DATA_DIR",
                )
            )
            checks.append(DoctorCheck(CHECK_MIGRATION, False, "无法检查（数据库不可用）"))
            return DoctorReport(tuple(checks))

    try:
        database_ok = check_database(resolved_engine)
        checks.append(
            DoctorCheck(CHECK_DATABASE, database_ok, "ok" if database_ok else "unavailable")
        )
        if not database_ok:
            checks.append(DoctorCheck(CHECK_MIGRATION, False, "无法检查（数据库不可用）"))
            return DoctorReport(tuple(checks))

        try:
            status = get_migration_status(resolved_engine)
        except Exception as exc:
            checks.append(DoctorCheck(CHECK_MIGRATION, False, f"无法读取迁移状态：{exc}"))
        else:
            checks.append(_migration_check(status))
        return DoctorReport(tuple(checks))
    finally:
        if owns_engine:
            resolved_engine.dispose()


__all__ = [
    "CHECK_CONFIG",
    "CHECK_DATABASE",
    "CHECK_MIGRATION",
    "DoctorCheck",
    "DoctorReport",
    "run_doctor",
]
