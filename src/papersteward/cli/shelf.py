"""P7 `shelf` CLI：待审特殊桶提名、接受与拒绝。"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from papersteward.core.entities import Paper, SpecialNominationRecord
from papersteward.core.enums import SpecialBucketKind
from papersteward.core.errors import DomainError
from papersteward.errors import AppError
from papersteward.pipeline.shelf import (
    ShelfDecision,
    accept_nomination,
    list_pending_nominations,
    reject_nomination,
)
from papersteward.storage import create_db_engine, create_session_factory, session_scope

KINDS = tuple(kind.value for kind in SpecialBucketKind)


def add_commands(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    shelf = subparsers.add_parser("shelf", help="特殊桶提名审核")
    commands = shelf.add_subparsers(dest="shelf_command", required=True)

    review = commands.add_parser("review", help="列出待审特殊桶提名")
    review.add_argument("--limit", type=int, default=50)
    review.add_argument("--offset", type=int, default=0)
    review.add_argument("--json", action="store_true")

    accept = commands.add_parser("accept", help="接受提名并归入特殊桶")
    accept.add_argument("paper_id")
    accept.add_argument("--kind", choices=KINDS, help="特殊桶类型；缺省按 review>classic>distant")
    accept.add_argument("--by", required=True, help="审核人")
    accept.add_argument("--note")
    accept.add_argument(
        "--force", action="store_true", help="覆盖未通过校验的提名（必须同时提供 --note）"
    )
    accept.add_argument("--json", action="store_true")

    reject = commands.add_parser("reject", help="拒绝提名并保留主题归属")
    reject.add_argument("paper_id")
    reject.add_argument("--kind", choices=KINDS, help="特殊桶类型；缺省按 review>classic>distant")
    reject.add_argument("--by", required=True, help="审核人")
    reject.add_argument("--note")
    reject.add_argument("--json", action="store_true")


def _record_json(record: SpecialNominationRecord, paper: Paper) -> dict[str, Any]:
    return {
        "paper_id": record.paper_id,
        "title": paper.title,
        "kind": record.kind.value,
        "status": record.status.value,
        "confidence": record.confidence,
        "reason": record.reason,
        "evidence": [{"quote": item.quote, "valid": item.valid} for item in record.evidence],
        "review_layers": list(record.review_layers),
        "classic_basis": list(record.classic_basis),
        "bridge_memo": dict(record.bridge_memo) if record.bridge_memo is not None else None,
        "gate_reasons": list(record.reasons),
        "provenance": {
            "model": record.provenance.model,
            "prompt_version": record.provenance.prompt_version,
            "generated_at": record.provenance.generated_at.isoformat(),
            "input_hash": record.provenance.input_hash,
        },
        "nominated_at": record.nominated_at.isoformat(),
        "decided_at": record.decided_at.isoformat() if record.decided_at else None,
        "decided_by": record.decided_by,
        "decision_note": record.decision_note,
        "paper_status": paper.status.value,
    }


def _print_review(pairs: list[tuple[SpecialNominationRecord, Paper]]) -> None:
    for record, paper in pairs:
        valid = sum(1 for item in record.evidence if item.valid)
        print(f"{record.paper_id} [{record.kind.value}] {paper.title}")
        print(f"  置信度 {record.confidence:.2f}；证据 {valid}/{len(record.evidence)} 有效")
        print(f"  理由：{record.reason}")
        if record.review_layers:
            print(f"  覆盖层级：{', '.join(record.review_layers)}")
        if record.classic_basis:
            print(f"  经典依据：{', '.join(record.classic_basis)}")
        if record.bridge_memo is not None:
            print(f"  桥接假设：{record.bridge_memo.get('shared_structure', '')}")
        if record.reasons:
            print(f"  未通过校验：{', '.join(record.reasons)}")
    if not pairs:
        print("特殊桶提名队列为空")


def _decision_json(decision: ShelfDecision) -> dict[str, Any]:
    payload = _record_json(decision.nomination, decision.paper)
    payload["bucket_id"] = decision.bucket.id if decision.bucket is not None else None
    payload["superseded"] = [
        {"paper_id": item.paper_id, "kind": item.kind.value} for item in decision.superseded
    ]
    return payload


def _print_decision(decision: ShelfDecision) -> None:
    record = decision.nomination
    suffix = f" → {decision.bucket.id}" if decision.bucket is not None else ""
    print(
        f"{record.paper_id}: {record.kind.value} {record.status.value}"
        f"（{decision.paper.status.value}）{suffix}"
    )
    if decision.superseded:
        kinds = ", ".join(item.kind.value for item in decision.superseded)
        print(f"  已作废其他待审提名：{kinds}")


def _kind(value: str | None) -> SpecialBucketKind | None:
    return SpecialBucketKind(value) if value is not None else None


def _run(args: argparse.Namespace) -> int:
    from papersteward.config import get_settings

    settings = get_settings()
    engine = create_db_engine(settings.db_url)
    try:
        factory = create_session_factory(engine)
        with session_scope(factory) as session:
            if args.shelf_command == "review":
                pairs, total = list_pending_nominations(
                    session, limit=args.limit, offset=args.offset
                )
                if args.json:
                    print(
                        json.dumps(
                            {
                                "items": [_record_json(record, paper) for record, paper in pairs],
                                "total": total,
                                "limit": args.limit,
                                "offset": args.offset,
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                else:
                    _print_review(pairs)
                return 0
            if args.shelf_command == "accept":
                decision = accept_nomination(
                    session,
                    args.paper_id,
                    kind=_kind(args.kind),
                    reviewed_by=args.by,
                    note=args.note,
                    force=args.force,
                )
            else:
                decision = reject_nomination(
                    session,
                    args.paper_id,
                    kind=_kind(args.kind),
                    reviewed_by=args.by,
                    note=args.note,
                )
            if args.json:
                print(json.dumps(_decision_json(decision), ensure_ascii=False, indent=2))
            else:
                _print_decision(decision)
            return 0
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
