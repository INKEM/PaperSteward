"""本地日报CLI：显式生成和投递，不启动常驻任务。"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from papersteward.config import get_settings
from papersteward.core.entities import utc_now
from papersteward.core.errors import DomainError
from papersteward.delivery.render import render_markdown
from papersteward.delivery.service import create_digest, deliver
from papersteward.errors import AppError
from papersteward.storage import create_db_engine, create_session_factory, session_scope


def add_commands(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("digest", help="生成日报；可显式写本地文件/发送已确认SMTP")
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--send-smtp", action="store_true")
    parser.add_argument("--recover-file", action="store_true")
    parser.add_argument("--allow-downgrade", action="store_true")
    parser.add_argument("--json", action="store_true")


def run(args: argparse.Namespace) -> int:
    try:
        settings = get_settings()
        engine = create_db_engine(settings.db_url)
        try:
            factory = create_session_factory(engine)
            now = utc_now()
            with session_scope(factory) as session:
                digest = create_digest(
                    session, args.date, now=now, allow_downgrade=args.allow_downgrade
                )
            results = []
            if args.out:
                results.append(
                    deliver(
                        factory,
                        args.date,
                        now=now,
                        settings=settings,
                        out=args.out,
                        recover_file=args.recover_file,
                    )
                )
            if args.send_smtp:
                results.append(
                    deliver(factory, args.date, now=now, settings=settings, channel="smtp")
                )
            if args.json:
                print(
                    json.dumps(
                        {"digest": digest.model_dump(mode="json"), "deliveries": results},
                        ensure_ascii=True,
                    )
                )
            else:
                # JSON转义终端控制字符，正文文件保持Markdown排版。
                print(json.dumps(render_markdown(digest), ensure_ascii=False))
            return 0
        finally:
            engine.dispose()
    except (DomainError, AppError) as exc:
        print(f"错误：{exc.message}；请检查G4/日期/投递状态。", file=sys.stderr)
    except (SQLAlchemyError, ValidationError, OSError):
        print(
            "错误：数据库/文件/配置不可用；检查配置并运行 alembic upgrade head。", file=sys.stderr
        )
    return 1
