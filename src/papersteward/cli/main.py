"""`papersteward` 命令行入口。"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from papersteward import __version__
from papersteward.cli.demo import seed_demo_data
from papersteward.cli.doctor import DoctorReport, run_doctor
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
    return parser


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
        if args.command == "doctor":
            return _run_doctor()
        if args.command == "demo" and getattr(args, "demo_command", None) == "seed":
            return _run_demo_seed()
    except KeyboardInterrupt:
        print("已取消", file=sys.stderr)
        return 130

    parser.error(f"未知命令：{args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
