"""P4 process/review CLI：离线处理与人工审核队列。"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from papersteward.config import LLMProviderKind
from papersteward.core.entities import Bucket, Paper, ResearchProfile, ReviewItem, Taxonomy, utc_now
from papersteward.core.enums import PaperStatus
from papersteward.core.errors import DomainError
from papersteward.errors import AppError, InvalidRequestError, NotFoundError
from papersteward.llm import FakeLLM, LLMGateway, build_llm_provider
from papersteward.llm.errors import LLMError
from papersteward.pipeline.context import bound_context
from papersteward.pipeline.embeddings import HashingEmbeddingProvider
from papersteward.pipeline.process import ensure_topic_buckets, process_paper
from papersteward.pipeline.review import (
    approve_review,
    list_open_reviews,
    relabel_review,
)
from papersteward.scoring.config import ScoringConfig, validate_scoring_config
from papersteward.storage import (
    PaperRepository,
    create_db_engine,
    create_session_factory,
    session_scope,
)
from papersteward.storage.repositories import ConfigRepository, TaxonomyRepository

FIXTURE_KEYS = ("relevance", "summary", "classification", "special_nomination")


def add_commands(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    process = subparsers.add_parser("process", help="离线处理 screened 文献")
    process.add_argument("--paper", help="只处理指定 paper_id；缺省处理 screened 批次")
    process.add_argument("--limit", type=int, default=10, help="批次上限（默认 10）")
    process.add_argument(
        "--llm-fixture", help="按 relevance/summary/classification/special 顺序回放"
    )
    process.add_argument("--live", action="store_true", help="显式允许真实 LLM")
    process.add_argument("--json", action="store_true", help="输出 JSON")

    review = subparsers.add_parser("review", help="审核队列")
    commands = review.add_subparsers(dest="review_command", required=True)
    listing = commands.add_parser("list", help="列出待审核项")
    listing.add_argument("--limit", type=int, default=50)
    listing.add_argument("--offset", type=int, default=0)
    listing.add_argument("--json", action="store_true")

    approve = commands.add_parser("approve", help="批准审核项")
    approve.add_argument("paper_id")
    approve.add_argument("--by", required=True, help="审核人")
    approve.add_argument("--note")
    approve.add_argument("--json", action="store_true")

    relabel = commands.add_parser("relabel", help="人工改主/副标签")
    relabel.add_argument("paper_id")
    relabel.add_argument("--primary", required=True, help="主标签 code")
    relabel.add_argument("--secondary", action="append", default=[], help="副标签 code，可重复")
    relabel.add_argument("--by", required=True, help="审核人")
    relabel.add_argument("--note")
    relabel.add_argument("--json", action="store_true")


def _fixture_provider(path: str) -> FakeLLM:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InvalidRequestError(f"无法读取 LLM fixture：{exc}") from exc
    if not isinstance(data, dict) or any(key not in data for key in FIXTURE_KEYS):
        raise InvalidRequestError("LLM fixture 必须包含 " + "/".join(FIXTURE_KEYS))
    responses: list[str] = []
    for key in FIXTURE_KEYS:
        value = data[key]
        responses.append(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
    return FakeLLM(responses, model_id="process-fixture")


def _processor_context(
    session: Session, now: datetime
) -> tuple[ResearchProfile, Taxonomy, ScoringConfig, dict[str, Bucket]]:
    profile, binding = bound_context(session, now, action="process", day=now.date())
    taxonomy = TaxonomyRepository(session).get(binding["taxonomy_id"], binding["taxonomy_version"])
    if taxonomy is None:
        raise InvalidRequestError("选定分类法不存在")
    config_row = ConfigRepository(session).get("scoring", binding["scoring_version"])
    if config_row is None:
        raise InvalidRequestError("选定评分配置不存在")
    config = validate_scoring_config(ScoringConfig.model_validate(config_row.value_json))
    return profile, taxonomy, config, ensure_topic_buckets(session, taxonomy)


def _run_process(args: argparse.Namespace) -> int:
    if args.llm_fixture and args.live:
        raise InvalidRequestError("--llm-fixture 与 --live 只能选择一种")
    if not args.llm_fixture and not args.live:
        raise InvalidRequestError("需要 --llm-fixture（离线）或 --live（真实 LLM）")
    if args.llm_fixture and not args.paper:
        raise InvalidRequestError("单一 LLM fixture 只支持 --paper；批量请逐篇处理或使用 --live")
    from papersteward.config import get_settings

    settings = get_settings()
    if args.live and settings.llm_provider is LLMProviderKind.FAKE:
        raise InvalidRequestError(
            "当前 LLM provider=fake；请设置 PAPERSTEWARD_LLM_PROVIDER=openai_compatible "
            "并配置模型，或使用 --llm-fixture"
        )
    provider = (
        _fixture_provider(args.llm_fixture)
        if args.llm_fixture
        else build_llm_provider(settings, live=True)
    )
    gateway = LLMGateway(
        provider,
        max_attempts=settings.llm_max_attempts,
        timeout_s=settings.llm_timeout_s,
    )
    now = utc_now()
    engine = create_db_engine(settings.db_url)
    try:
        factory = create_session_factory(engine)
        with session_scope(factory) as session:
            profile, taxonomy, config, buckets = _processor_context(session, now)
            papers_repo = PaperRepository(session)
            papers = []
            if args.paper:
                paper = papers_repo.get(args.paper)
                if paper is None:
                    raise NotFoundError("文献不存在")
                papers = [paper]
            else:
                papers = [
                    paper
                    for paper in papers_repo.list(limit=args.limit)
                    if paper.status is PaperStatus.SCREENED
                ][: args.limit]
            outcomes = [
                process_paper(
                    session,
                    paper,
                    profile=profile,
                    taxonomy=taxonomy,
                    topic_buckets=buckets,
                    config=config,
                    gateway=gateway,
                    embedding_provider=HashingEmbeddingProvider(),
                    now=now,
                )
                for paper in papers
            ]
            payload = [outcome.as_json() for outcome in outcomes]
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                for outcome in outcomes:
                    flag = "review" if outcome.needs_review else "classified"
                    print(
                        f"{outcome.paper_id}: {flag} "
                        f"R={outcome.relevance.R:.1f} A={outcome.static.authority:.1f} "
                        f"E={outcome.static.extra_value:.1f} S={outcome.static.static_score:.1f} "
                        f"({len(outcome.reasons)} reasons)"
                    )
                if not outcomes:
                    print("没有可处理的 screened 文献")
            return 0
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            close()
        engine.dispose()


def _review_json(item: ReviewItem, paper: Paper) -> dict[str, Any]:
    return {
        "paper_id": item.paper_id,
        "title": paper.title,
        "status": paper.status.value,
        "reasons": list(item.reasons),
        "review_status": item.status.value,
        "resolution": item.resolution.value if item.resolution else None,
        "resolved_by": item.resolved_by,
        "note": item.note,
    }


def _run_review(args: argparse.Namespace) -> int:
    from papersteward.config import get_settings

    settings = get_settings()
    engine = create_db_engine(settings.db_url)
    try:
        factory = create_session_factory(engine)
        with session_scope(factory) as session:
            if args.review_command == "list":
                pairs, total = list_open_reviews(session, limit=args.limit, offset=args.offset)
                items = [_review_json(item, paper) for item, paper in pairs]
                if args.json:
                    print(
                        json.dumps(
                            {
                                "items": items,
                                "total": total,
                                "limit": args.limit,
                                "offset": args.offset,
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                else:
                    for entry in items:
                        print(f"{entry['paper_id']}: {', '.join(entry['reasons'])}")
                    if not items:
                        print("审核队列为空")
                return 0
            if args.review_command == "approve":
                item = approve_review(session, args.paper_id, reviewed_by=args.by, note=args.note)
            else:
                item = relabel_review(
                    session,
                    args.paper_id,
                    primary_tag=args.primary,
                    secondary_tags=args.secondary,
                    reviewed_by=args.by,
                    note=args.note,
                )
            paper = PaperRepository(session).get(args.paper_id)
            if paper is None:
                raise NotFoundError("文献不存在")
            payload = _review_json(item, paper)
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(f"{payload['paper_id']}: {payload['resolution']} → {payload['status']}")
            return 0
    finally:
        engine.dispose()


def run(args: argparse.Namespace) -> int:
    try:
        if args.command == "process":
            return _run_process(args)
        return _run_review(args)
    except (AppError, DomainError) as exc:
        message = exc.message if hasattr(exc, "message") else str(exc)
        print(f"错误：{message}", file=sys.stderr)
        return 1
    except LLMError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except (SQLAlchemyError, ValidationError, OSError):
        print(
            "错误：数据库未迁移/不可用或配置非法；请先运行 `alembic upgrade head`。",
            file=sys.stderr,
        )
        return 1


__all__ = ["add_commands", "run"]
