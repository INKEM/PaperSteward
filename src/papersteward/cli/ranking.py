"""P5 本地评分/队列CLI；不自动迁移或发送。"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from pydantic import BaseModel, ValidationError
from sqlalchemy.exc import SQLAlchemyError

from papersteward.config import get_settings
from papersteward.core.entities import utc_now
from papersteward.core.errors import DomainError
from papersteward.errors import AppError
from papersteward.scoring.queue import create_snapshots, explain_score, read_queue
from papersteward.storage import create_db_engine, create_session_factory, session_scope


def add_commands(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    queue = subparsers.add_parser("queue", help="查看已生成的优先级队列")
    queue.add_argument("--date", required=True, type=date.fromisoformat)
    queue.add_argument("--limit", type=int, default=50)
    queue.add_argument("--offset", type=int, default=0)
    queue.add_argument("--json", action="store_true")
    score = subparsers.add_parser("score", help="单篇分数解释")
    command = score.add_subparsers(dest="score_command", required=True).add_parser("explain")
    command.add_argument("paper_id")
    command.add_argument("--date", type=date.fromisoformat)
    command.add_argument("--json", action="store_true")
    scores = subparsers.add_parser("scores", help="生成不可变日快照")
    command = scores.add_subparsers(dest="scores_command", required=True).add_parser("snapshot")
    command.add_argument("--date", required=True, type=date.fromisoformat)
    command.add_argument("--json", action="store_true")


def run(args: argparse.Namespace) -> int:
    try:
        settings = get_settings()
        engine = create_db_engine(settings.db_url)
        try:
            factory = create_session_factory(engine)
            with session_scope(factory) as session:
                now = utc_now()
                result: BaseModel
                if args.command == "scores":
                    result = create_snapshots(session, args.date, now=now)
                elif args.command == "score":
                    result = explain_score(session, args.paper_id, now=now, day=args.date)
                else:
                    result = read_queue(
                        session, args.date, now=now, limit=args.limit, offset=args.offset
                    )
                # 使用JSON转义控制字符，终端不会执行论文文本中的转义序列。
                print(result.model_dump_json(indent=2))
            return 0
        finally:
            engine.dispose()
    except (AppError, DomainError) as exc:
        print(f"错误：{exc.message}；请检查初始化/分类/快照日期。", file=sys.stderr)
    except (SQLAlchemyError, ValidationError, OSError):
        print(
            "错误：数据库或配置不可用；请先运行 alembic upgrade head 并检查配置。", file=sys.stderr
        )
    return 1
