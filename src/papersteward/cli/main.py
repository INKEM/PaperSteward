"""`papersteward` 命令行入口。"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import date
from typing import cast

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from papersteward import __version__
from papersteward.cli.archive import add_commands as add_archive_commands
from papersteward.cli.archive import run as run_archive_command
from papersteward.cli.config import (
    run_config_changes,
    run_config_confirm,
    run_config_import,
    run_config_preview,
    run_config_show,
    run_config_validate,
    run_schema_show,
    run_schema_validate,
    schema_names,
)
from papersteward.cli.delivery import add_commands as add_delivery_commands
from papersteward.cli.delivery import run as run_delivery
from papersteward.cli.demo import seed_demo_data
from papersteward.cli.doctor import DoctorReport, run_doctor
from papersteward.cli.documents import (
    DocumentKind,
    run_archive,
    run_confirm,
    run_diff,
    run_import,
    run_validate,
)
from papersteward.cli.ingestion import add_commands as add_ingestion_commands
from papersteward.cli.ingestion import run as run_ingestion
from papersteward.cli.jobs import add_commands as add_jobs_commands
from papersteward.cli.jobs import run as run_jobs
from papersteward.cli.llm import run_llm_ping
from papersteward.cli.processing import add_commands as add_processing_commands
from papersteward.cli.processing import run as run_processing
from papersteward.cli.ranking import add_commands as add_ranking_commands
from papersteward.cli.ranking import run as run_ranking
from papersteward.cli.reading import add_commands as add_reading_commands
from papersteward.cli.reading import run as run_reading
from papersteward.cli.shelf import add_commands as add_shelf_commands
from papersteward.cli.shelf import run as run_shelf
from papersteward.cli.start import add_command as add_start_command
from papersteward.cli.start import run as run_start
from papersteward.cli.views import add_commands as add_view_commands
from papersteward.cli.views import run as run_views
from papersteward.config import format_settings_error


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="papersteward",
        description="本地优先的文献自动挖掘、优先级排序与每日推送系统",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "doctor",
        help="检查配置、数据库连接与迁移版本",
    )

    demo = subparsers.add_parser("demo", help="演示数据操作")
    demo_subparsers = demo.add_subparsers(dest="demo_command", required=True)
    demo_subparsers.add_parser(
        "seed",
        help="插入 p001 演示文献（幂等）",
    )

    add_start_command(subparsers)
    add_ingestion_commands(subparsers)
    add_processing_commands(subparsers)
    add_shelf_commands(subparsers)
    add_view_commands(subparsers)
    add_ranking_commands(subparsers)
    add_delivery_commands(subparsers)
    add_reading_commands(subparsers)
    add_archive_commands(subparsers)
    add_jobs_commands(subparsers)
    _add_document_commands(subparsers, "profile")
    _add_document_commands(subparsers, "taxonomy")

    doc = subparsers.add_parser("doc", help="文档版本比较")
    doc_subparsers = doc.add_subparsers(dest="doc_command", required=True)
    diff = doc_subparsers.add_parser("diff", help="比较两个文件或两个数据库版本")
    diff.add_argument("kind", choices=("profile", "taxonomy"))
    diff.add_argument("left", help="左文件路径，或数据库版本号")
    diff.add_argument("right", help="右文件路径，或数据库版本号")
    diff.add_argument("--id", dest="document_id", help="按数据库版本比较时的文档 ID")
    diff.add_argument("--json", action="store_true", help="输出 JSON")

    config = subparsers.add_parser("config", help="评分配置校验与版本导入")
    config_subparsers = config.add_subparsers(dest="config_command", required=True)
    config_validate = config_subparsers.add_parser("validate", help="校验 scoring YAML")
    config_validate.add_argument("path", help="scoring.yaml 路径")
    config_validate.add_argument("--json", action="store_true", help="输出 JSON")
    config_import = config_subparsers.add_parser("import", help="写入配置版本并记录事件")
    config_import.add_argument("path", help="scoring.yaml 路径")
    config_import.add_argument("--at", help="更新时间（ISO 8601，带时区）")

    config_preview = config_subparsers.add_parser("preview", help="预览全局权重影响")
    source = config_preview.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", help="完整 scoring.yaml")
    source.add_argument("--set", action="append", default=[], metavar="PATH=VALUE")
    config_preview.add_argument("--version", help="新版本号；缺省自动提升")
    config_preview.add_argument("--date", type=date.fromisoformat, help="预览队列日期")
    config_preview.add_argument("--effective-on", type=date.fromisoformat)
    config_preview.add_argument("--limit", type=int, default=20)
    config_preview.add_argument("--json", action="store_true")

    config_confirm = config_subparsers.add_parser("confirm", help="确认并激活权重变更")
    config_confirm.add_argument("change_id")
    config_confirm.add_argument("--by", required=True, help="确认人")
    config_confirm.add_argument("--note")
    config_confirm.add_argument("--json", action="store_true")

    config_changes = config_subparsers.add_parser("changes", help="列出权重变更记录")
    config_changes.add_argument("--limit", type=int, default=50)
    config_changes.add_argument("--json", action="store_true")

    config_show = config_subparsers.add_parser("show", help="查看权重变更记录")
    config_show.add_argument("change_id")
    config_show.add_argument("--json", action="store_true")

    schema = subparsers.add_parser("schema", help="Agent 输出 JSON schema")
    schema_subparsers = schema.add_subparsers(dest="schema_command", required=True)
    schema_show = schema_subparsers.add_parser("show", help="输出 JSON Schema")
    schema_show.add_argument("name", choices=schema_names(), help="schema 名称")
    schema_validate = schema_subparsers.add_parser("validate", help="校验 JSON 文件")
    schema_validate.add_argument("name", choices=schema_names(), help="schema 名称")
    schema_validate.add_argument("path", help="JSON 文件路径")

    llm = subparsers.add_parser("llm", help="LLM 网关检查")
    llm_subparsers = llm.add_subparsers(dest="llm_command", required=True)
    ping = llm_subparsers.add_parser("ping", help="检查 LLM provider 连通性")
    ping.add_argument("--fake", action="store_true", help="仅开发/测试：使用 FakeLLM")
    ping.add_argument(
        "--live", action="store_true", help="显式允许真实 OpenAI-compatible HTTP 调用"
    )
    ping.add_argument("--json", action="store_true", help="输出 JSON")
    return parser


def _add_document_commands(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    name: str,
) -> None:
    parser = subparsers.add_parser(name, help=f"{name} 文档操作")
    commands = parser.add_subparsers(dest=f"{name}_command", required=True)

    validate = commands.add_parser("validate", help="解析并校验 Markdown 文档")
    validate.add_argument("path", help="Markdown 文件路径")
    validate.add_argument("--json", action="store_true", help="输出 JSON")

    import_parser = commands.add_parser("import", help="解析并写入 draft 版本")
    import_parser.add_argument("path", help="Markdown 文件路径")
    import_parser.add_argument("--id", dest="document_id", help="覆盖文档 ID")

    confirm = commands.add_parser("confirm", help="确认 draft 版本")
    confirm.add_argument("version", help="版本号，例如 1.0")
    confirm.add_argument("--id", dest="document_id", required=True, help="文档 ID")
    confirm.add_argument("--at", help="确认时间（ISO 8601，带时区）")

    archive = commands.add_parser("archive", help="归档已确认版本")
    archive.add_argument("version", help="版本号，例如 1.0")
    archive.add_argument("--id", dest="document_id", required=True, help="文档 ID")
    archive.add_argument("--at", help="归档时间（ISO 8601，带时区）")


def _print_doctor_report(report: DoctorReport) -> None:
    print(f"PaperSteward {__version__}")
    for check in report.checks:
        lines = check.detail.splitlines() or [""]
        marker = "OK" if check.ok else "FAIL"
        print(f"[{marker}] {check.name}: {lines[0]}")
        for line in lines[1:]:
            print(f"       {line}")
    print("结果：通过" if report.ok else "结果：失败")


def _run_doctor() -> int:
    report = run_doctor()
    _print_doctor_report(report)
    return 0 if report.ok else 1


def _run_demo_seed() -> int:
    try:
        result = seed_demo_data()
    except ValidationError as exc:
        print(format_settings_error(exc), file=sys.stderr)
        return 1
    except (SQLAlchemyError, OSError):
        print(
            "错误：数据库尚未迁移或不可用。请先运行 `alembic upgrade head`。",
            file=sys.stderr,
        )
        return 1

    if result.created:
        print(f"已插入演示文献 {result.paper_id}（当前共 {result.total} 篇）")
    else:
        print(f"演示文献 {result.paper_id} 已存在（当前共 {result.total} 篇）")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "start":
            return run_start(args)
        if args.command == "digest":
            return run_delivery(args)
        if args.command == "archive":
            return run_archive_command(args)
        if args.command == "jobs":
            return run_jobs(args)
        if args.command in {"done", "reading"}:
            return run_reading(args)
        if args.command in {"ingest", "runs"}:
            return run_ingestion(args)
        if args.command in {"queue", "score", "scores"}:
            return run_ranking(args)
        if args.command in {"process", "review"}:
            return run_processing(args)
        if args.command == "shelf":
            return run_shelf(args)
        if args.command == "view":
            return run_views(args)
        if args.command == "doctor":
            return _run_doctor()
        if args.command == "demo" and getattr(args, "demo_command", None) == "seed":
            return _run_demo_seed()
        if args.command in {"profile", "taxonomy"}:
            kind = cast(DocumentKind, args.command)
            subcommand = getattr(args, f"{args.command}_command")
            if subcommand == "validate":
                return run_validate(kind, args.path, json_output=args.json)
            if subcommand == "import":
                return run_import(kind, args.path, document_id=args.document_id)
            if subcommand == "confirm":
                return run_confirm(kind, args.version, document_id=args.document_id, at=args.at)
            if subcommand == "archive":
                return run_archive(kind, args.version, document_id=args.document_id, at=args.at)
        if args.command == "doc" and getattr(args, "doc_command", None) == "diff":
            return run_diff(
                cast(DocumentKind, args.kind),
                args.left,
                args.right,
                document_id=args.document_id,
                json_output=args.json,
            )
        if args.command == "config":
            if args.config_command == "validate":
                return run_config_validate(args.path, json_output=args.json)
            if args.config_command == "import":
                return run_config_import(args.path, at=args.at)
            if args.config_command == "preview":
                return run_config_preview(
                    path=args.file,
                    overrides=args.set,
                    version=args.version,
                    at=args.date,
                    effective_on=args.effective_on,
                    limit=args.limit,
                    json_output=args.json,
                )
            if args.config_command == "confirm":
                return run_config_confirm(
                    args.change_id, by=args.by, note=args.note, json_output=args.json
                )
            if args.config_command == "changes":
                return run_config_changes(limit=args.limit, json_output=args.json)
            if args.config_command == "show":
                return run_config_show(args.change_id, json_output=args.json)
        if args.command == "schema":
            if args.schema_command == "show":
                return run_schema_show(args.name)
            if args.schema_command == "validate":
                return run_schema_validate(args.name, args.path)
        if args.command == "llm" and args.llm_command == "ping":
            return run_llm_ping(fake=args.fake, live=args.live, json_output=args.json)
    except KeyboardInterrupt:
        print("已取消", file=sys.stderr)
        return 130

    parser.error(f"未知命令：{args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
