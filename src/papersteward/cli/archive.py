"""本地归档列表/导出。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from fastapi.encoders import jsonable_encoder
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from papersteward.config import get_settings
from papersteward.core.errors import DomainError
from papersteward.delivery.channels import write_file
from papersteward.errors import AppError
from papersteward.reading.archive import list_archives
from papersteward.reading.export import export_archives
from papersteward.storage import create_db_engine, create_session_factory, session_scope


def add_commands(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    commands = subparsers.add_parser("archive", help="归档查询和导出").add_subparsers(
        dest="archive_command", required=True
    )
    listing = commands.add_parser("list")
    listing.add_argument("--tag")
    listing.add_argument("--min-score", type=int, choices=range(1, 6))
    listing.add_argument("--query")
    listing.add_argument("--limit", type=int, default=50)
    listing.add_argument("--offset", type=int, default=0)
    listing.add_argument("--json", action="store_true")
    export = commands.add_parser("export")
    export.add_argument("--format", choices=("bibtex", "csv", "markdown", "json"), required=True)
    export.add_argument("--out", type=Path, required=True)


def run(args: argparse.Namespace) -> int:
    try:
        engine = create_db_engine(get_settings().db_url)
        try:
            with session_scope(create_session_factory(engine)) as session:
                if args.archive_command == "list":
                    page = list_archives(
                        session,
                        limit=args.limit,
                        offset=args.offset,
                        tag=args.tag,
                        min_score=args.min_score,
                        query=args.query,
                    )
                    print(json.dumps(jsonable_encoder(page), ensure_ascii=True))
                else:
                    records = []
                    offset = 0
                    while True:
                        page = list_archives(session, limit=200, offset=offset)
                        records.extend(page["items"])
                        offset += len(page["items"])
                        if offset >= page["total"]:
                            break
                    write_file(args.out, export_archives(records, args.format))
                    print(
                        json.dumps(
                            {"exported": len(records), "out": str(args.out)}, ensure_ascii=True
                        )
                    )
            return 0
        finally:
            engine.dispose()
    except (DomainError, AppError) as exc:
        print(f"错误：{exc.message}", file=sys.stderr)
    except (SQLAlchemyError, ValidationError, OSError):
        print("错误：数据库/输出不可用；检查迁移和输出路径。", file=sys.stderr)
    return 1
