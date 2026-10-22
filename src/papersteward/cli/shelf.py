"""P7 `shelf` CLI：待审提名、接受/拒绝、档位评估/覆盖与轮换计划。"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from typing import Any

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from papersteward.core.entities import Paper, SpecialNominationRecord, SpecialTierSnapshot
from papersteward.core.enums import ScoreTier, SpecialBucketKind
from papersteward.core.errors import DomainError
from papersteward.delivery.shelf_view import (
    ShelfItem,
    ShelfPage,
    ShelfSummary,
    list_shelf,
    parse_tier_filter,
    shelf_item_for_paper,
    shelf_summaries,
)
from papersteward.delivery.special import (
    SpecialRotationPlan,
    load_special_candidates,
    load_special_quotas,
    plan_special_rotation,
)
from papersteward.errors import AppError, InvalidRequestError, NotFoundError
from papersteward.pipeline.shelf import (
    ShelfDecision,
    ShelfPromotion,
    accept_nomination,
    list_pending_nominations,
    promote_to_topic,
    reject_nomination,
    reopen_nomination,
)
from papersteward.scoring.special import assess_special, override_special_tier
from papersteward.storage import create_db_engine, create_session_factory, session_scope
from papersteward.storage.repositories import PaperRepository

KINDS = tuple(kind.value for kind in SpecialBucketKind)


def add_commands(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    shelf = subparsers.add_parser("shelf", help="特殊桶提名、档位与轮换")
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

    reopen = commands.add_parser("reopen", help="把已拒绝的特殊桶提名重新打开为待审")
    reopen.add_argument("paper_id")
    reopen.add_argument("--kind", choices=KINDS, help="特殊桶类型；缺省按 review>classic>distant")
    reopen.add_argument("--by", required=True, help="审核人")
    reopen.add_argument("--note")
    reopen.add_argument("--json", action="store_true")

    promote = commands.add_parser("promote", help="把特殊桶文献提升回主标签主题桶")
    promote.add_argument("paper_id")
    promote.add_argument("--by", required=True, help="审核人")
    promote.add_argument("--note")
    promote.add_argument("--json", action="store_true")

    reject = commands.add_parser("reject", help="拒绝提名并保留主题归属")
    reject.add_argument("paper_id")
    reject.add_argument("--kind", choices=KINDS, help="特殊桶类型；缺省按 review>classic>distant")
    reject.add_argument("--by", required=True, help="审核人")
    reject.add_argument("--note")
    reject.add_argument("--json", action="store_true")

    assess = commands.add_parser("assess", help="重新评估特殊桶档位并追加快照")
    assess.add_argument("paper_id")
    assess.add_argument("--json", action="store_true")

    tier = commands.add_parser("tier", help="人工覆盖特殊桶档位")
    tier.add_argument("paper_id")
    tier.add_argument("--tier", required=True, choices=("high", "medium", "low"))
    tier.add_argument("--by", required=True, help="审核人")
    tier.add_argument("--note", required=True, help="改档理由")
    tier.add_argument("--json", action="store_true")

    plan = commands.add_parser("plan", help="生成某日特殊桶轮换计划")
    plan.add_argument("--date", required=True, type=date.fromisoformat, help="YYYY-MM-DD")
    plan.add_argument("--used-review", type=int, default=0, help="本周 review 已用配额")
    plan.add_argument("--used-distant", type=int, default=0, help="本周 distant 已用配额")
    plan.add_argument("--used-classic", type=int, default=0, help="本周 classic 已用配额")
    plan.add_argument("--json", action="store_true")

    listing = commands.add_parser("list", help="查看三个特殊桶或指定桶")
    listing.add_argument("--kind", choices=KINDS)
    _add_view_args(listing)

    distant = commands.add_parser("distant", help="查看远邻领域特殊桶")
    _add_view_args(distant)

    classic = commands.add_parser("classic", help="查看经典/奠基特殊桶")
    _add_view_args(classic)

    show = commands.add_parser("show", help="查看特殊桶文献详情")
    show.add_argument("paper_id")
    show.add_argument("--json", action="store_true")


def _add_view_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--tier", choices=("high", "medium", "low", "unrated"))
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--explain", action="store_true", help="显示档位说明与 kind 专属证据")
    parser.add_argument("--json", action="store_true")


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


def _tier_json(snapshot: SpecialTierSnapshot) -> dict[str, Any]:
    return {
        "paper_id": snapshot.paper_id,
        "nomination_id": snapshot.nomination_id,
        "kind": snapshot.kind.value,
        "value": snapshot.value,
        "tier": snapshot.tier.value,
        "dimensions": dict(snapshot.dimensions),
        "weights": dict(snapshot.weights),
        "reasons": list(snapshot.reasons),
        "explanation": list(snapshot.explanation),
        "evidence": [{"quote": item.quote, "valid": item.valid} for item in snapshot.evidence],
        "breakdown": dict(snapshot.breakdown_json),
        "rubric_version": snapshot.rubric_version,
        "assigned_by": snapshot.assigned_by.value,
        "reviewed_by": snapshot.reviewed_by,
        "note": snapshot.note,
        "created_at": snapshot.created_at.isoformat(),
    }


def _print_tier(snapshot: SpecialTierSnapshot) -> None:
    print(
        f"{snapshot.paper_id}: {snapshot.kind.value} → {snapshot.tier.value} "
        f"({snapshot.assigned_by.value})"
    )
    for line in snapshot.explanation:
        print(f"  {line}")


def _plan_json(plan: SpecialRotationPlan) -> dict[str, Any]:
    return plan.as_json()


def _print_plan(plan: SpecialRotationPlan) -> None:
    print(f"{plan.day.isoformat()} 特殊桶计划（上限 {plan.max_slots}）：")
    for slot in plan.slots:
        print(f"  [{slot.kind.value}/{slot.tier.value}] {slot.paper_id} {slot.title}")
    if not plan.slots:
        print("  今日无合格特殊桶内容（不凑数）")
    for reason in plan.skipped:
        print(f"  跳过：{reason}")
    used = ", ".join(f"{kind.value}={plan.used[kind]}/{plan.quotas[kind]}" for kind in plan.quotas)
    print(f"  当周用量：{used}")


def _decision_json(decision: ShelfDecision) -> dict[str, Any]:
    payload = _record_json(decision.nomination, decision.paper)
    payload["bucket_id"] = decision.bucket.id if decision.bucket is not None else None
    payload["tier"] = decision.tier.tier.value if decision.tier is not None else None
    payload["tier_value"] = decision.tier.value if decision.tier is not None else None
    payload["superseded"] = [
        {"paper_id": item.paper_id, "kind": item.kind.value} for item in decision.superseded
    ]
    return payload


def _promotion_json(promotion: ShelfPromotion) -> dict[str, Any]:
    return {
        "paper_id": promotion.paper.id,
        "title": promotion.paper.title,
        "paper_status": promotion.paper.status.value,
        "bucket_id": promotion.bucket.id,
        "bucket_type": promotion.bucket.bucket_type.value,
        "tag_code": promotion.bucket.tag_code,
        "score_date": promotion.static.score_date.isoformat(),
        "config_version": promotion.static.config_version,
        "static_score": promotion.static.static_score,
    }


def _print_promotion(promotion: ShelfPromotion) -> None:
    print(
        f"{promotion.paper.id}: 已提升到 {promotion.bucket.id}"
        f"（{promotion.paper.status.value}），静态分 {promotion.static.static_score:.1f}"
        f" @ {promotion.static.score_date}"
    )


def _print_decision(decision: ShelfDecision) -> None:
    record = decision.nomination
    suffix = f" → {decision.bucket.id}" if decision.bucket is not None else ""
    tier = f"；档位 {decision.tier.tier.value}" if decision.tier is not None else ""
    print(
        f"{record.paper_id}: {record.kind.value} {record.status.value}"
        f"（{decision.paper.status.value}）{suffix}{tier}"
    )
    if decision.superseded:
        kinds = ", ".join(item.kind.value for item in decision.superseded)
        print(f"  已作废其他待审提名：{kinds}")


def _print_summary(summary: ShelfSummary) -> None:
    print(
        f"{summary.kind.value} {summary.name}：共 {summary.total} 篇，"
        f"high {summary.high} / medium {summary.medium} / low {summary.low} / "
        f"未评估 {summary.unrated}，周上限 {summary.quota}"
    )


def _print_item(item: ShelfItem, *, explain: bool) -> None:
    tier = item.tier.tier.value if item.tier is not None else "未评估"
    print(f"  {item.paper.id} [{tier}] {item.paper.title}")
    print(
        f"    主标签 {item.primary_tag or '—'} · 状态 {item.paper.status.value}"
        f" · 归属 {item.kind.value}"
    )
    if item.nomination is not None:
        print(f"    理由：{item.nomination.reason}")
    if item.needs_assessment:
        print("    尚未评估：运行 papersteward shelf assess <paper_id>")
    for evidence in item.evidence:
        marker = "有效" if evidence.valid else "未核对"
        print(f"    证据（{marker}）：{evidence.quote}")
    if not explain:
        return
    for line in item.tier.explanation if item.tier is not None else ():
        print(f"    档位：{line}")
    if item.nomination is not None and item.nomination.review_layers:
        print(f"    覆盖层级：{', '.join(item.nomination.review_layers)}")
    if item.nomination is not None and item.nomination.classic_basis:
        print(f"    经典依据：{', '.join(item.nomination.classic_basis)}")
    if item.nomination is not None and item.nomination.bridge_memo is not None:
        print("    桥接假设：")
        for key, label in (
            ("source_mechanism", "源机制"),
            ("shared_structure", "共享结构"),
            ("transfer_experiment", "迁移实验"),
            ("falsification_signal", "证伪信号"),
        ):
            value = item.nomination.bridge_memo.get(key)
            if isinstance(value, str) and value.strip():
                print(f"      {label}：{value}")
    if item.tier is not None and item.tier.note:
        print(f"    改档备注：{item.tier.note}")


def _print_page(page: ShelfPage, *, explain: bool = False) -> None:
    _print_summary(page.summary)
    for item in page.items:
        _print_item(item, explain=explain)
    if not page.items:
        print("  暂无内容（不占位）")


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
                if args.json:
                    print(json.dumps(_decision_json(decision), ensure_ascii=False, indent=2))
                else:
                    _print_decision(decision)
                return 0
            if args.shelf_command == "reopen":
                reopened = reopen_nomination(
                    session,
                    args.paper_id,
                    kind=_kind(args.kind),
                    reviewed_by=args.by,
                    note=args.note,
                )
                paper = PaperRepository(session).get(args.paper_id)
                assert paper is not None
                if args.json:
                    print(
                        json.dumps(
                            _record_json(reopened, paper),
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                else:
                    print(
                        f"{reopened.paper_id}: {reopened.kind.value} 已重新打开为"
                        f" {reopened.status.value}"
                    )
                return 0
            if args.shelf_command == "promote":
                promotion = promote_to_topic(
                    session,
                    args.paper_id,
                    reviewed_by=args.by,
                    note=args.note,
                )
                if args.json:
                    print(json.dumps(_promotion_json(promotion), ensure_ascii=False, indent=2))
                else:
                    _print_promotion(promotion)
                return 0
            if args.shelf_command == "reject":
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
            if args.shelf_command == "assess":
                snapshot = assess_special(session, args.paper_id)
                if args.json:
                    print(json.dumps(_tier_json(snapshot), ensure_ascii=False, indent=2))
                else:
                    _print_tier(snapshot)
                return 0
            if args.shelf_command == "tier":
                snapshot = override_special_tier(
                    session,
                    args.paper_id,
                    tier=ScoreTier(args.tier),
                    reviewed_by=args.by,
                    note=args.note,
                )
                if args.json:
                    print(json.dumps(_tier_json(snapshot), ensure_ascii=False, indent=2))
                else:
                    _print_tier(snapshot)
                return 0
            if args.shelf_command == "plan":
                used = {kind: getattr(args, f"used_{kind.value}") for kind in SpecialBucketKind}
                plan = plan_special_rotation(
                    load_special_candidates(session, as_of=args.date),
                    day=args.date,
                    quotas=load_special_quotas(session),
                    used=used,
                )
                if args.json:
                    print(json.dumps(_plan_json(plan), ensure_ascii=False, indent=2))
                else:
                    _print_plan(plan)
                return 0
            if args.shelf_command == "list":
                if args.kind is None:
                    if args.tier is not None or args.explain:
                        raise InvalidRequestError("--tier/--explain 需要同时指定 --kind")
                    summaries = shelf_summaries(session)
                    if args.json:
                        print(
                            json.dumps(
                                {
                                    "items": [summary.as_json() for summary in summaries],
                                    "total": len(summaries),
                                    "limit": len(summaries),
                                    "offset": 0,
                                },
                                ensure_ascii=False,
                                indent=2,
                            )
                        )
                    else:
                        for summary in summaries:
                            _print_summary(summary)
                    return 0
                page = list_shelf(
                    session,
                    SpecialBucketKind(args.kind),
                    tier=parse_tier_filter(args.tier),
                    limit=args.limit,
                    offset=args.offset,
                )
                if args.json:
                    print(json.dumps(page.as_json(), ensure_ascii=False, indent=2))
                else:
                    _print_page(page, explain=args.explain)
                return 0
            if args.shelf_command in {"distant", "classic"}:
                kind = (
                    SpecialBucketKind.DISTANT
                    if args.shelf_command == "distant"
                    else SpecialBucketKind.CLASSIC
                )
                page = list_shelf(
                    session,
                    kind,
                    tier=parse_tier_filter(args.tier),
                    limit=args.limit,
                    offset=args.offset,
                )
                if args.json:
                    print(json.dumps(page.as_json(), ensure_ascii=False, indent=2))
                else:
                    _print_page(page, explain=args.explain)
                return 0
            if args.shelf_command == "show":
                item = shelf_item_for_paper(session, args.paper_id)
                if item is None:
                    raise NotFoundError("文献不在特殊桶")
                if args.json:
                    print(json.dumps(item.as_json(), ensure_ascii=False, indent=2))
                else:
                    _print_item(item, explain=True)
                return 0
            raise DomainError("shelf.command", f"未知命令：{args.shelf_command}")
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
