from __future__ import annotations

import json

from fastapi.testclient import TestClient
from test_special_shelf import NOW, PAPER, _process, _setup

from papersteward.api import create_app
from papersteward.cli.main import main
from papersteward.config import get_settings
from papersteward.core.entities import (
    NominationEvidence,
    Paper,
    PaperBucket,
    PaperTag,
    Provenance,
    SpecialNominationRecord,
)
from papersteward.core.enums import (
    BucketRole,
    PaperStatus,
    SpecialBucketKind,
    SpecialNominationStatus,
    TagRole,
)
from papersteward.pipeline.shelf import accept_nomination, ensure_special_buckets
from papersteward.scoring.special import assess_special
from papersteward.storage import (
    BucketRepository,
    PaperBucketRepository,
    PaperRepository,
    PaperTagRepository,
    SpecialNominationRepository,
    SpecialTierRepository,
    create_session_factory,
    session_scope,
)

TITLE = "Distant <script>alert(1)</script> paper"
PROVENANCE = Provenance("fixture", "special_nomination/v1", NOW, "0" * 64)


def _seed_special(
    session,
    paper_id: str,
    kind: SpecialBucketKind,
    *,
    title: str,
    bridge_memo: dict[str, str] | None = None,
    classic_basis: tuple[str, ...] = (),
    review_layers: tuple[str, ...] = ("method", "evaluation"),
    confidence: float = 0.8,
    reasons: tuple[str, ...] = (),
) -> None:
    bucket = ensure_special_buckets(session)[kind]
    PaperRepository(session).add(
        Paper(
            id=paper_id,
            title=title,
            status=PaperStatus.SHELVED,
            first_seen_at=NOW,
        )
    )
    PaperTagRepository(session).add(PaperTag(paper_id, "T02", TagRole.PRIMARY, "1.0"))
    PaperBucketRepository(session).add(PaperBucket(paper_id, bucket.id, BucketRole.FILING))
    nomination = SpecialNominationRecord(
        id=f"nom-{paper_id}",
        paper_id=paper_id,
        kind=kind,
        status=SpecialNominationStatus.ACCEPTED,
        confidence=confidence,
        reason=f"{kind.value} 合成理由",
        evidence=(NominationEvidence(f"quote-{paper_id}", True),),
        review_layers=review_layers,
        classic_basis=classic_basis,
        bridge_memo=bridge_memo,
        reasons=reasons,
        provenance=PROVENANCE,
        nominated_at=NOW,
        decided_at=NOW,
        decided_by="fixture-user",
    )
    SpecialNominationRepository(session).upsert(nomination)
    assess_special(session, paper_id, nomination=nomination, now=NOW)


def _seed_all(migrated_engine) -> None:
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        _setup(session)
        _process(session)
        accept_nomination(session, PAPER.id, reviewed_by="fixture-user", note="合成审核", now=NOW)
        _seed_special(
            session,
            "review-low",
            SpecialBucketKind.REVIEW,
            title="Low review paper",
            review_layers=(),
            confidence=0.2,
            reasons=("low_confidence",),
        )
        _seed_special(
            session,
            "distant-1",
            SpecialBucketKind.DISTANT,
            title=TITLE,
            bridge_memo={
                "source_mechanism": "源机制说明",
                "shared_structure": "共享结构说明",
                "transfer_experiment": "迁移实验说明",
                "falsification_signal": "证伪信号说明",
            },
        )
        _seed_special(
            session,
            "classic-1",
            SpecialBucketKind.CLASSIC,
            title="Classic paper",
            review_layers=(),
            classic_basis=("foundational",),
        )


def _client(engine) -> TestClient:
    return TestClient(create_app(engine=engine))


def test_api_overview_filters_and_detail(migrated_engine):
    _seed_all(migrated_engine)
    with _client(migrated_engine) as client:
        overview = client.get("/shelves").json()
        assert overview["total"] == 3
        assert [item["kind"] for item in overview["items"]] == [
            "review",
            "distant",
            "classic",
        ]
        review = overview["items"][0]
        assert review["total"] == 2
        assert (review["medium"], review["low"], review["unrated"]) == (1, 1, 0)
        assert review["quota"] == 3

        first = client.get("/shelves/review?limit=1").json()
        assert first["total"] == 2 and first["limit"] == 1
        assert first["items"][0]["tier"] == "medium"
        second = client.get("/shelves/review?limit=1&offset=1").json()
        assert second["items"][0]["tier"] == "low"
        assert client.get("/shelves/review?tier=high").json()["total"] == 0
        assert client.get("/shelves/review?tier=urgent").status_code == 422

        distant = client.get("/shelves/distant").json()["items"][0]
        assert distant["bridge_memo"]["source_mechanism"] == "源机制说明"
        assert distant["kind"] == "distant"
        assert "value" not in distant
        assert "priority" not in distant
        assert "rank_score" not in distant
        assert distant["excluded_from_priority"] is True

        classic = client.get("/shelves/classic").json()["items"][0]
        assert classic["classic_basis"] == ["foundational"]

        detail = client.get(f"/shelves/review/{PAPER.id}").json()
        assert detail["paper_id"] == PAPER.id
        assert detail["review_layers"] == ["method", "evaluation"]
        assert detail["tier"] == "medium"
        assert "value" not in detail

        missing = client.get("/shelves/nope")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "not_found"


def test_api_missing_shelf_is_empty_and_read_only(migrated_engine):
    factory = create_session_factory(migrated_engine)
    with _client(migrated_engine) as client:
        overview = client.get("/shelves").json()
        assert [item["total"] for item in overview["items"]] == [0, 0, 0]
        assert all(item["bucket_id"] is None for item in overview["items"])
        page = client.get("/shelves/review").json()
        assert page["items"] == [] and page["shelf"]["total"] == 0
    with session_scope(factory) as session:
        assert BucketRepository(session).list() == []


def test_api_reads_do_not_write_tiers(migrated_engine):
    _seed_all(migrated_engine)
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        before = len(SpecialTierRepository(session).history(PAPER.id))
    with _client(migrated_engine) as client:
        for path in ("/shelves", "/shelves/review", f"/shelves/review/{PAPER.id}"):
            assert client.get(path).status_code == 200
    with session_scope(factory) as session:
        assert len(SpecialTierRepository(session).history(PAPER.id)) == before


def test_page_renders_tiers_evidence_and_escapes_titles(migrated_engine):
    _seed_all(migrated_engine)
    with _client(migrated_engine) as client:
        page = client.get("/ui/shelves")
        assert page.status_code == 200
        html = page.text
        assert "跨层综述" in html and "远邻领域" in html and "经典/奠基" in html
        assert "桥接假设" in html
        assert "源机制说明" in html
        assert "foundational" in html
        assert "Distant &lt;script&gt;alert(1)&lt;/script&gt; paper" in html
        assert "<script>alert(1)</script>" not in html
        assert "rank_score" not in html

        detail = client.get("/ui/papers/distant-1")
        assert detail.status_code == 200
        assert "桥接假设" in detail.text
        assert "excluded_from_priority" not in detail.text
        assert "score_date" not in detail.text


def test_page_empty_shelves_show_no_placeholder(migrated_engine):
    with _client(migrated_engine) as client:
        page = client.get("/ui/shelves")
        assert page.status_code == 200
        # UXR-17：空收藏给出收录说明与返回队列入口。
        assert "该收藏暂无论文" in page.text
        assert "返回阅读队列" in page.text
        assert "不占常规队列容量" in page.text
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        assert BucketRepository(session).list() == []


def test_cli_lists_and_shows_shelves(migrated_engine, monkeypatch, capsys):
    _seed_all(migrated_engine)
    monkeypatch.setenv("PAPERSTEWARD_DATABASE_URL", str(migrated_engine.url))
    get_settings.cache_clear()
    try:
        assert main(["shelf", "list", "--json"]) == 0
        overview = json.loads(capsys.readouterr().out)
        assert overview["total"] == 3

        assert main(["shelf", "distant", "--explain", "--json"]) == 0
        distant = json.loads(capsys.readouterr().out)
        assert distant["total"] == 1
        assert distant["items"][0]["bridge_memo"]["falsification_signal"] == "证伪信号说明"

        assert main(["shelf", "show", "classic-1", "--json"]) == 0
        classic = json.loads(capsys.readouterr().out)
        assert classic["classic_basis"] == ["foundational"]

        assert main(["shelf", "list", "--kind", "review", "--tier", "medium", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["total"] == 1

        assert main(["shelf", "classic"]) == 0
        assert "经典/奠基" in capsys.readouterr().out

        assert main(["shelf", "list", "--tier", "high"]) == 1
        assert "需要同时指定" in capsys.readouterr().err
        assert main(["shelf", "show", "not-special"]) == 1
        assert "不在特殊桶" in capsys.readouterr().err
    finally:
        get_settings.cache_clear()


def test_page_shelf_filter_and_unrated_notice(migrated_engine):
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        bucket = ensure_special_buckets(session)[SpecialBucketKind.REVIEW]
        PaperRepository(session).add(
            Paper(
                id="unrated-1",
                title="Unrated special paper",
                status=PaperStatus.SHELVED,
                first_seen_at=NOW,
            )
        )
        PaperTagRepository(session).add(PaperTag("unrated-1", "T02", TagRole.PRIMARY, "1.0"))
        PaperBucketRepository(session).add(PaperBucket("unrated-1", bucket.id, BucketRole.FILING))
    with _client(migrated_engine) as client:
        page = client.get("/ui/shelves?kind=review")
        assert page.status_code == 200
        assert "未评估" in page.text
        assert "shelf assess" in page.text
        api = client.get("/shelves/review?tier=unrated").json()
        assert api["total"] == 1
        assert api["items"][0]["needs_assessment"] is True
        assert api["items"][0]["tier"] is None


def test_cli_show_requires_special_bucket(migrated_engine, monkeypatch, capsys):
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        PaperRepository(session).add(
            Paper(id="plain-1", title="Plain paper", status=PaperStatus.SHELVED)
        )
    monkeypatch.setenv("PAPERSTEWARD_DATABASE_URL", str(migrated_engine.url))
    get_settings.cache_clear()
    try:
        assert main(["shelf", "show", "plain-1"]) == 1
        assert "不在特殊桶" in capsys.readouterr().err
    finally:
        get_settings.cache_clear()
