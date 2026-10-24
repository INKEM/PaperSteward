"""阅读操作CLI，不调用网络或外部模型。"""

from __future__ import annotations

import argparse
import json
import sys
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from papersteward.config import get_settings
from papersteward.core.entities import utc_now
from papersteward.core.errors import DomainError
from papersteward.delivery.contracts import Action
from papersteward.errors import AppError
from papersteward.reading.actions import apply_action
from papersteward.storage import create_db_engine, create_session_factory, session_scope


def _arguments(parser: argparse.ArgumentParser, kind: str) -> None:
    parser.add_argument("paper_id")
    parser.add_argument("--request-id", help="重试时使用同一ID")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(action_kind=kind)
    if kind == "done":
        parser.add_argument("--mode", choices=("deep", "skim"), required=True)
    if kind in {"done", "score"}:
        parser.add_argument("--score", type=int, choices=range(1, 6), required=kind == "score")
    if kind in {"done", "note", "reject"}:
        parser.add_argument("--note", required=kind in {"note", "reject"})


def add_commands(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    _arguments(subparsers.add_parser("done", help="标记读完并保存评分/备注"), "done")
    commands = subparsers.add_parser("reading", help="阅读反馈").add_subparsers(
        dest="reading_command", required=True
    )
    for kind in ("score", "note", "reject"):
        _arguments(commands.add_parser(kind), kind)


def run(args: argparse.Namespace) -> int:
    try:
        values = {k: getattr(args, k) for k in ("mode", "score", "note") if hasattr(args, k)}
        request = Action.model_validate(
            {"request_id": args.request_id or uuid4().hex, "kind": args.action_kind, **values}
        )
        settings = get_settings()
        engine = create_db_engine(settings.db_url)
        try:
            with session_scope(create_session_factory(engine)) as session:
                result = apply_action(session, args.paper_id, request, now=utc_now())
            print(json.dumps(result, ensure_ascii=True))
            return 0
        finally:
            engine.dispose()
    except (DomainError, AppError) as exc:
        print(f"错误：{exc.message}；检查文献状态和请求ID。", file=sys.stderr)
    except (SQLAlchemyError, ValidationError, OSError):
        print("错误：数据库/输入不可用；检查初始化、迁移和操作字段。", file=sys.stderr)
    return 1
