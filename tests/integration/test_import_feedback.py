"""导入已读论文的评分信号闭环回归（IMP-05）。

三条通道零改动复用的验证：偏好分 U（周冻结窗口内不即时变、跨周可解释变化）、
挖掘规划品味块（近 8 周评分即时进 prompt）、标签补给饥饿度 H（有标签计入、
无标签不计入）；以及无标签导入论文的修订全链路。
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from conftest import activate_workspace
from papersteward.core.entities import (
    Bucket,
    Paper,
    PaperBucket,
    PaperTag,
    Provenance,
    StaticScoreSnapshot,
    Summary,
)
from papersteward.core.enums import (
    BucketRole,
    BucketType,
    PaperStatus,
    SummaryValidationStatus,
    TagRole,
)
from papersteward.delivery.contracts import Action, ImportReadPaperInput
from papersteward.llm import FakeLLM, LLMGateway
from papersteward.reading.actions import apply_action
from papersteward.reading.archive import list_archives, read_archive
from papersteward.reading.import_service import import_read_paper
from papersteward.scoring.queue import create_snapshots, explain_score
from papersteward.sources.planner import plan_queries
from papersteward.storage import create_session_factory, session_scope
from papersteward.storage.models import EventRow, RatingRow
from papersteward.storage.repositories import (
    BucketRepository,
    PaperBucketRepository,
    PaperRepository,
    PaperTagRepository,
    StaticScoreSnapshotRepository,
    SummaryRepository,
)

# 激活在周三；导入在其后两周（仍周三）；解释越过一个周日（周一）。
EARLY = datetime(2026, 9, 2, 8, tzinfo=UTC)
IMPORT_TIME = EARLY + timedelta(days=14)
NEXT_MONDAY = EARLY + timedelta(days=19)
PLANNING = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures/llm/query_planning.valid.json").read_text()
)


@pytest.fixture
def factory(migrated_engine):
    activate_workspace(migrated_engine, now=EARLY)
    return create_session_factory(migrated_engine)


def _seed_queue_paper(session, paper_id: str, now: datetime, r: int = 80) -> None:
    """已分类+摘要+静态快照齐备的队列候选（test_priority_queue.seed 的可定时版）。"""
    if BucketRepository(session).get("b-t01") is None:
        BucketRepository(session).add(Bucket("b-t01", "Topic", BucketType.TOPIC, tag_code="T01"))
    PaperRepository(session).add(
        Paper(
            paper_id,
            paper_id,
            status=PaperStatus.CLASSIFIED,
            first_seen_at=now,
            published_at=now,
        )
    )
    PaperTagRepository(session).add(PaperTag(paper_id, "T01", TagRole.PRIMARY, "1.0"))
    PaperBucketRepository(session).add(PaperBucket(paper_id, "b-t01", BucketRole.FILING))
    SummaryRepository(session).save(
        Summary(
            paper_id,
            "做方法并验证结论",
            "what",
            "how",
            "finding",
            "limitation",
            evidence=("evidence",),
            validation_status=SummaryValidationStatus.PASSED,
            provenance=Provenance("fixture", "v1", now, "0" * 64),
        )
    )
    StaticScoreSnapshotRepository(session).append(
        StaticScoreSnapshot(
            paper_id=paper_id,
            score_date=now.date(),
            relevance=r,
            authority=50,
            extra_value=50,
            static_score=round(0.65 * r + 0.35 * 50, 1),
            confidence=0.5,
            config_version="1.0.0",
            profile_version="1.0",
            taxonomy_version="1.0",
            input_hash="0" * 64,
            created_at=now,
            breakdown_json={},
        )
    )


def _import(session, paper_title: str, *, score: int, primary_tag: str | None = "T01") -> str:
    return import_read_paper(
        session,
        ImportReadPaperInput.model_validate(
            {"title": paper_title, "score": score, "primary_tag": primary_tag}
        ),
        now=IMPORT_TIME,
    )


def _explain(factory, paper_id: str, now: datetime):
    with session_scope(factory) as session:
        create_snapshots(session, now.date(), now=now)
    with session_scope(factory) as session:
        return explain_score(session, paper_id, now=now)


def test_import_ratings_change_preference_across_week_boundary(factory) -> None:
    with session_scope(factory) as session:
        _seed_queue_paper(session, "q1", EARLY)
        create_snapshots(session, EARLY.date(), now=EARLY)
        baseline = explain_score(session, "q1", now=EARLY)
        assert set(baseline.components) == {"R", "A", "E", "F", "H", "U"}
        assert baseline.scores["U"] == 50
        assert baseline.quasi["ratings_count"] == 0

        for index in range(3):
            _import(session, f"外部已读高分论文 {index}", score=5)

        # 周冻结：同周导入的评分不即时改变 U（cutoff 排他）。
        same_week = explain_score(session, "q1", now=IMPORT_TIME)
        assert same_week.scores["U"] == 50

    crossed = _explain(factory, "q1", NEXT_MONDAY)
    assert crossed.scores["U"] > 50
    assert crossed.quasi["ratings_count"] == 3
    assert crossed.quasi["completed_count"] == 3
    # 可解释：quasi 分解里同标签亲和度被拉高（评分全部 5 分）；reasons 仅在
    # 数据降级（覆盖不足/无评分）时非空，健康数据下为空是预期。
    assert crossed.quasi["tag"] > 50


def test_imported_ratings_feed_planner_taste_block(factory) -> None:
    with session_scope(factory) as session:
        _import(session, "外部已读高分论文", score=5)
        _import(session, "外部已读低分论文", score=1)
        fake = FakeLLM([json.dumps(PLANNING, ensure_ascii=False)], model_id="fake-llm")
        gateway = LLMGateway(fake, clock=lambda: IMPORT_TIME)
        plan_queries(session, gateway, today=IMPORT_TIME.date())
    prompt = fake.calls[0]
    # 品味块包含导入论文的标题、主标签与评分（高低分都进 prompt，低分即抑制信号）。
    assert "《外部已读高分论文》" in prompt and "评分 5/5" in prompt
    assert "《外部已读低分论文》" in prompt and "评分 1/5" in prompt
    assert "主标签 T01" in prompt
    assert "近期低评分论文的主题不要再挖" in prompt


def test_hunger_counts_tagged_import_and_ignores_tagless(factory) -> None:
    with session_scope(factory) as session:
        _seed_queue_paper(session, "q1", EARLY)
        _import(session, "外部已读有标签论文", score=4, primary_tag="T01")
        _import(session, "外部已读无标签论文", score=4, primary_tag=None)

    detail = _explain(factory, "q1", IMPORT_TIME)
    # 有标签完成计入 H（同标签完成 → 饥饿度下降）；无标签完成不计入任何桶。
    assert detail.dynamic["completed_count"] == 1
    assert detail.dynamic["hunger"] < 50


def test_hunger_ignores_tagless_import_alone(factory) -> None:
    with session_scope(factory) as session:
        _seed_queue_paper(session, "q1", EARLY)
        _import(session, "外部已读无标签论文", score=4, primary_tag=None)

    detail = _explain(factory, "q1", IMPORT_TIME)
    assert detail.dynamic["completed_count"] == 0
    assert detail.dynamic["hunger"] == 50


def test_tagless_import_revision_full_chain(factory) -> None:
    with session_scope(factory) as session:
        paper_id = _import(session, "外部已读待修订论文", score=4, primary_tag=None)

    with session_scope(factory) as session:
        before = read_archive(session, paper_id)
        result = apply_action(
            session,
            paper_id,
            Action(kind="score", request_id="rev-1", score=2),
            now=IMPORT_TIME + timedelta(seconds=1),
        )
        assert result["status"] == "archived"

    with session_scope(factory) as session:
        after = read_archive(session, paper_id)
        assert after.content == before.content, "归档快照不可因修订改变"
        assert after.score == 2
        assert len(after.revision_history) == 2
        ratings = list(session.scalars(select(RatingRow).where(RatingRow.paper_id == paper_id)))
        assert len(ratings) == 2
        rated = session.scalars(
            select(EventRow).where(
                EventRow.paper_id == paper_id,
                EventRow.event_type == "paper.rated",
            )
        ).all()
        assert len(rated) == 1
        # 无标签论文的筛选与列表：修订后的评分参与 min_score 过滤。
        assert list_archives(session, min_score=3)["total"] == 0
        assert list_archives(session, min_score=1)["total"] == 1
