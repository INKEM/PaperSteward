"""离线周度偏好任务CLI。"""

from __future__ import annotations

import argparse
import json
import sys

from fastapi.encoders import jsonable_encoder
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from papersteward.api.jobs import WeeklyRequest
from papersteward.config import get_settings
from papersteward.core.entities import utc_now
from papersteward.core.enums import RunStatus
from papersteward.core.errors import DomainError
from papersteward.errors import AppError
from papersteward.jobs.preference import refresh_weekly
from papersteward.storage import create_db_engine, create_session_factory, session_scope


def add_commands(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    commands = subparsers.add_parser("jobs", help="显式运行周期任务").add_subparsers(
        dest="job_command", required=True
    )
    command = commands.add_parser("preference-weekly", help="检查阅读评分并更新未读候选U")
    command.add_argument("--run-id")
    command.add_argument("--json", action="store_true")


def run(args: argparse.Namespace) -> int:
    try:
        request = WeeklyRequest(run_id=args.run_id)
        engine = create_db_engine(get_settings().db_url)
        try:
            with session_scope(create_session_factory(engine)) as session:
                report = refresh_weekly(session, now=utc_now(), run_id=request.run_id)
            print(json.dumps(jsonable_encoder(report), ensure_ascii=True))
            return 0 if report.status is RunStatus.SUCCEEDED else 1
        finally:
            engine.dispose()
    except (DomainError, AppError) as exc:
        print(f"错误：{exc.message}；检查初始化确认、计划时段和运行ID。", file=sys.stderr)
    except (SQLAlchemyError, ValidationError, OSError):
        print("错误：数据库/配置不可用；检查迁移和任务输入。", file=sys.stderr)
    return 1
