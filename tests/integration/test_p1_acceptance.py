from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.engine import Engine

from papersteward.core.entities import Bucket, Paper, PaperBucket, PaperTag, Summary
from papersteward.core.enums import (
    AssignedBy,
    BucketRole,
    BucketType,
    SummaryValidationStatus,
    TagRole,
)
from papersteward.core.invariants import ensure_paper_classification
from papersteward.documents import (
    confirm_profile,
    confirm_taxonomy,
    import_profile,
    import_taxonomy,
)
from papersteward.llm import FakeLLM, LLMGateway
from papersteward.schemas import ClassificationOutput, SummaryOutput
from papersteward.scoring import import_scoring_config, read_scoring_config
from papersteward.storage import (
    BucketRepository,
    ConfigRepository,
    EventRepository,
    PaperBucketRepository,
    PaperRepository,
    PaperTagRepository,
    ProfileRepository,
    SummaryRepository,
    TaxonomyRepository,
    create_session_factory,
    session_scope,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
PROFILE = FIXTURES / "profiles" / "basic.md"
TAXONOMY = FIXTURES / "taxonomies" / "basic.md"
CONFIG = FIXTURES / "configs" / "scoring.yaml"
CLASSIFICATION = (FIXTURES / "llm" / "classification.valid.json").read_text(encoding="utf-8")
SUMMARY = (FIXTURES / "llm" / "summary.valid.json").read_text(encoding="utf-8")

T1 = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
T2 = datetime(2026, 10, 21, 9, 5, tzinfo=UTC)
T3 = datetime(2026, 10, 21, 9, 10, tzinfo=UTC)


def test_p1_end_to_end_contract_on_migrated_schema(migrated_engine: Engine) -> None:
    factory = create_session_factory(migrated_engine)

    with session_scope(factory) as session:
        # 1. profile / taxonomy / config 版本与确认事件
        import_profile(session, PROFILE)
        confirm_profile(session, "demo-profile", "1.0", confirmed_at=T1)
        import_taxonomy(session, TAXONOMY)
        confirm_taxonomy(session, "demo-taxonomy", "1.0", confirmed_at=T1)
        import_scoring_config(session, CONFIG, updated_at=T2)

        profile = ProfileRepository(session).latest_confirmed("demo-profile")
        taxonomy = TaxonomyRepository(session).latest_confirmed("demo-taxonomy")
        assert profile is not None and profile.version == "1.0"
        assert taxonomy is not None and len(taxonomy.tags) == 7
        assert read_scoring_config(session) is not None

        # 2. 文献与主题桶
        PaperRepository(session).add(Paper(id="p001", title="A long-document RAG paper"))
        bucket = Bucket(
            id="b-t02",
            name="检索增强生成",
            bucket_type=BucketType.TOPIC,
            tag_code="T02",
        )
        BucketRepository(session).add(bucket)

        # 3. FakeLLM 分类输出 -> schema -> 标签/归属桶；非法 JSON 不会走到这里
        gateway = LLMGateway(
            FakeLLM([CLASSIFICATION], model_id="fake-llm"),
            clock=lambda: T3,
        )
        classified = gateway.complete_json(
            "classification",
            {
                "taxonomy": taxonomy.content_md,
                "title": "A long-document RAG paper",
                "abstract": "A paper about retrieval-augmented generation.",
            },
        )
        assert isinstance(classified.data, ClassificationOutput)
        primary_data = classified.data
        primary = PaperTag(
            paper_id="p001",
            tag_code=primary_data.primary_tag,
            role=TagRole.PRIMARY,
            taxonomy_version=taxonomy.version,
            confidence=primary_data.primary_confidence,
            assigned_by=AssignedBy.AGENT,
            provenance=classified.provenance,
        )
        secondary = PaperTag(
            paper_id="p001",
            tag_code=primary_data.secondary_tags[0].name,
            role=TagRole.SECONDARY,
            taxonomy_version=taxonomy.version,
            confidence=primary_data.secondary_tags[0].confidence,
            assigned_by=AssignedBy.AGENT,
            provenance=classified.provenance,
        )
        PaperTagRepository(session).add_many((primary, secondary))
        filing = PaperBucket(
            paper_id="p001",
            bucket_id="b-t02",
            role=BucketRole.FILING,
            assigned_by=AssignedBy.AGENT,
        )
        PaperBucketRepository(session).add(filing)

        tags = PaperTagRepository(session).list_for_paper("p001")
        buckets = PaperBucketRepository(session).list_for_paper("p001")
        ensure_paper_classification(
            tags=tags,
            buckets=buckets,
            taxonomy=taxonomy,
            buckets_by_id={"b-t02": bucket},
            paper_id="p001",
        )

        # 4. FakeLLM 摘要输出 -> Summary（带 provenance/evidence）
        summary_gateway = LLMGateway(
            FakeLLM([SUMMARY], model_id="fake-llm"),
            clock=lambda: T3,
        )
        summary_result = summary_gateway.complete_json(
            "summary",
            {
                "title": "A long-document RAG paper",
                "abstract": "A paper about retrieval-augmented generation.",
            },
        )
        assert isinstance(summary_result.data, SummaryOutput)
        summary_data = summary_result.data
        summary = Summary(
            paper_id="p001",
            summary_100=summary_data.summary_100,
            what=summary_data.what,
            how=summary_data.how,
            finding=summary_data.finding,
            limitation=summary_data.limitation,
            evidence=tuple(item.quote for item in summary_data.evidence),
            validation_status=SummaryValidationStatus.PASSED,
            provenance=summary_result.provenance,
        )
        SummaryRepository(session).save(summary)

    # 5. 重新打开会话验证持久化与审计
    with session_scope(factory) as session:
        paper = PaperRepository(session).get("p001")
        assert paper is not None
        tags = PaperTagRepository(session).list_for_paper("p001")
        assert {tag.role for tag in tags} == {TagRole.PRIMARY, TagRole.SECONDARY}
        assert PaperTagRepository(session).list_for_paper("p001")[0].provenance is not None

        buckets = PaperBucketRepository(session).list_for_paper("p001")
        assert [(item.bucket_id, item.role) for item in buckets] == [("b-t02", BucketRole.FILING)]
        stored_summary = SummaryRepository(session).get("p001")
        assert stored_summary == summary
        assert stored_summary is not None and stored_summary.provenance is not None

        assert ConfigRepository(session).get("scoring", "1.0.0") is not None
        event_types = {
            event.event_type.value for event in EventRepository(session).recent(limit=20)
        }
        assert {
            "profile.confirmed",
            "taxonomy.confirmed",
            "config.updated",
        } <= event_types
