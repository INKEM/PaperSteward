"""P7 `view` CLI：自定义排序预设、预览与保存。"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from typing import Any

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from papersteward.core.entities import ViewConfig, utc_now
from papersteward.core.errors import DomainError
from papersteward.errors import AppError, InvalidRequestError
from papersteward.scoring.views import (
    PRESET_ORDER,
    ViewPreset,
    ViewPreview,
    delete_view_config,
    formula,
    get_view_config,
    list_presets,
    list_view_configs,
    preview_view,
    save_view,
)
from papersteward.storage import create_db_engine, create_session_factory, session_scope


def add_commands(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    view = subparsers.add_parser("view", help="自定义排序视图")
    commands = view.add_subparsers(dest="view_command", required=True)

    presets = commands.add_parser("presets", help="列出内置视图预设")
    presets.add_argument("--json", action="store_true")

    preview = commands.add_parser("preview", help="预览当前队列的自定义排序")
    group = preview.add_mutually_exclusive_group()
    group.add_argument("--preset", choices=PRESET_ORDER)
    group.add_argument("--name", help="已保存视图名称")
    group.add_argument("--view-id", help="已保存视图 ID")
    group.add_argument("--weights", help="R:35,E:40,F:15,A:10 或 JSON 对象")
    preview.add_argument("--date", type=date.fromisoformat, help="YYYY-MM-DD；缺省为今天")
    preview.add_argument("--limit", type=int, default=20)
    preview.add_argument("--offset", type=int, default=0)
    preview.add_argument("--json", action="store_true")

    save = commands.add_parser("save", help="保存自定义视图")
    save.add_argument("--name", required=True)
    group = save.add_mutually_exclusive_group(required=True)
    group.add_argument("--preset", choices=PRESET_ORDER)
    group.add_argument("--weights", help="R:35,E:40,F:15,A:10 或 JSON 对象")
    save.add_argument("--description")
    save.add_argument("--json", action="store_true")

    listing = commands.add_parser("list", help="列出已保存视图")
    listing.add_argument("--json", action="store_true")

    show = commands.add_parser("show", help="查看已保存视图")
    show.add_argument("target", help="视图 ID 或名称")
    show.add_argument("--json", action="store_true")

    delete = commands.add_parser("delete", help="删除已保存视图")
    delete.add_argument("target", help="视图 ID 或名称")
    delete.add_argument("--json", action="store_true")


def _parse_weights(text: str) -> dict[str, float]:
    raw = text.strip()
    if not raw:
        raise InvalidRequestError("weights 不能为空")
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise InvalidRequestError("weights JSON 无效") from exc
        if not isinstance(data, dict):
            raise InvalidRequestError("weights JSON 必须是对象")
        try:
            return {str(key): float(value) for key, value in data.items()}
        except (TypeError, ValueError) as exc:
            raise InvalidRequestError("weights JSON 的值必须是数字") from exc
    result: dict[str, float] = {}
    for part in raw.split(","):
        key, separator, value = part.partition(":")
        if not separator or not key.strip() or not value.strip():
            raise InvalidRequestError("weights 格式应为 R:35,E:40")
        normalized = key.strip().upper()
        if normalized in result:
            raise InvalidRequestError(f"组件重复：{normalized}")
        try:
            result[normalized] = float(value)
        except ValueError as exc:
            raise InvalidRequestError(f"权重 {normalized} 不是数字") from exc
    return result


def _preset_json(item: ViewPreset) -> dict[str, Any]:
    return item.as_json()


def _config_json(item: ViewConfig) -> dict[str, Any]:
    return {
        "id": item.id,
        "name": item.name,
        "weights": dict(item.weights),
        "preset": item.preset,
        "description": item.description,
        "formula": formula(item.weights),
        "created_at": item.created_at.isoformat(),
    }


def _print_config(item: ViewConfig) -> None:
    print(
        f"{item.name} ({item.id})"
        + (f" · preset={item.preset}" if item.preset is not None else "")
        + f"\n  公式：{formula(item.weights)}"
    )
    if item.description:
        print(f"  说明：{item.description}")


def _print_preview(preview: ViewPreview) -> None:
    view = preview.view
    print(f"视图：{view.label}（{view.name}）")
    print(f"日期：{preview.score_date} · 公式：{formula(view.weights)}")
    print(
        f"候选 {preview.candidate_total} 篇；{preview.changed} 篇名次变化；"
        f"显示 {len(preview.items)} 篇（offset {preview.offset}）"
    )
    for item in preview.items:
        moved = (
            "—"
            if item.delta_rank == 0
            else (f"↑{item.delta_rank}" if item.delta_rank > 0 else f"↓{-item.delta_rank}")
        )
        print(
            f"#{item.rank} {item.paper_id} {moved} view={item.view_score:.1f}"
            f"（默认 #{item.default_rank}，P={item.default_priority:.1f}）{item.title}"
        )
        parts = [
            f"{key} {component.score:.1f}×{component.weight:.2f}"
            for key, component in item.components.items()
            if component.weight > 0
        ]
        print("  " + " · ".join(parts))


def _run(args: argparse.Namespace) -> int:
    from papersteward.config import get_settings

    settings = get_settings()
    engine = create_db_engine(settings.db_url)
    try:
        factory = create_session_factory(engine)
        with session_scope(factory) as session:
            if args.view_command == "presets":
                preset_items = list_presets()
                if args.json:
                    print(
                        json.dumps(
                            {
                                "items": [_preset_json(item) for item in preset_items],
                                "total": len(preset_items),
                                "limit": len(preset_items),
                                "offset": 0,
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                else:
                    for item in preset_items:
                        weights = " / ".join(f"{key} {item.weights[key]:g}" for key in item.weights)
                        print(f"{item.name} {item.label}：{weights}")
                return 0
            if args.view_command == "list":
                saved_views = list_view_configs(session)
                if args.json:
                    print(
                        json.dumps(
                            {
                                "items": [_config_json(config) for config in saved_views],
                                "total": len(saved_views),
                                "limit": len(saved_views),
                                "offset": 0,
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                else:
                    for config in saved_views:
                        _print_config(config)
                return 0
            if args.view_command == "save":
                parsed_weights = _parse_weights(args.weights) if args.weights is not None else None
                view = save_view(
                    session,
                    args.name,
                    preset=args.preset,
                    weights=parsed_weights,
                    description=args.description,
                )
                if args.json:
                    print(json.dumps(_config_json(view), ensure_ascii=False, indent=2))
                else:
                    print(f"已保存视图 {view.name} ({view.id})")
                return 0
            if args.view_command == "show":
                view = get_view_config(session, args.target)
                if args.json:
                    print(json.dumps(_config_json(view), ensure_ascii=False, indent=2))
                else:
                    _print_config(view)
                return 0
            if args.view_command == "delete":
                view = delete_view_config(session, args.target)
                if args.json:
                    print(json.dumps(_config_json(view), ensure_ascii=False, indent=2))
                else:
                    print(f"已删除视图 {view.name} ({view.id})")
                return 0
            if args.view_command == "preview":
                preview_weights = _parse_weights(args.weights) if args.weights is not None else None
                view_id = args.view_id
                if args.name is not None:
                    view_id = get_view_config(session, args.name).id
                preview = preview_view(
                    session,
                    args.date or utc_now().date(),
                    preset=args.preset,
                    weights=preview_weights,
                    view_id=view_id,
                    limit=args.limit,
                    offset=args.offset,
                )
                if args.json:
                    print(json.dumps(preview.as_json(), ensure_ascii=False, indent=2))
                else:
                    _print_preview(preview)
                return 0
            raise InvalidRequestError(f"未知视图命令：{args.view_command}")
    finally:
        engine.dispose()


def run(args: argparse.Namespace) -> int:
    try:
        return _run(args)
    except (AppError, DomainError) as exc:
        message = getattr(exc, "message", str(exc))
        print(f"错误：{message}", file=sys.stderr)
        return 1
    except (SQLAlchemyError, ValidationError, OSError):
        print(
            "错误：数据库未迁移/不可用或配置非法；请先运行 `alembic upgrade head`。",
            file=sys.stderr,
        )
        return 1


__all__ = ["add_commands", "run"]
