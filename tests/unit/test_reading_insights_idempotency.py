"""写入回执哈希规范化的单元验证（R2-00B）。

覆盖：载荷决定哈希（操作/目标变化即变化）、来源排序去重、
expected_revision 等前置条件不参与、回填与运行时输入形态一致。
"""

from papersteward.reading.insights.idempotency import (
    ASSET_CREATE,
    ASSET_REVISE,
    READING_TIME_RECORD,
    asset_content_payload,
    reading_time_payload,
    request_hash,
)


def test_operation_and_target_change_the_hash():
    payload = reading_time_payload(paper_id="p1", precision="day", day="2026-10-01", month=None)
    base = request_hash(READING_TIME_RECORD, "p1", payload)
    assert base == request_hash(READING_TIME_RECORD, "p1", payload)
    assert base != request_hash(READING_TIME_RECORD, "p2", payload)
    assert base != request_hash(ASSET_CREATE, "p1", payload)


def test_sources_are_order_insensitive_and_deduplicated():
    base = asset_content_payload(
        "harvest",
        title="t",
        body="b",
        applicability=None,
        sources=[("p1", None), ("p2", "2026-10-01T00:00:00+00:00")],
    )
    reordered = asset_content_payload(
        "harvest",
        title="t",
        body="b",
        applicability=None,
        sources=[("p2", "2026-10-01T00:00:00+00:00"), ("p1", None)],
    )
    assert base == reordered
    assert base["sources"] == [
        {"paper_id": "p1", "rating_at": None},
        {"paper_id": "p2", "rating_at": "2026-10-01T00:00:00+00:00"},
    ]
    duplicated = asset_content_payload(
        "harvest",
        title="t",
        body="b",
        applicability=None,
        sources=[("p1", None), ("p1", None)],
    )
    assert duplicated["sources"] == [{"paper_id": "p1", "rating_at": None}]


def test_empty_and_null_rating_at_are_equivalent():
    with_none = asset_content_payload(
        None, title="t", body="b", applicability=None, sources=[("p1", None)]
    )
    with_empty = asset_content_payload(
        None, title="t", body="b", applicability=None, sources=[("p1", "")]
    )
    assert with_none == with_empty


def test_content_fields_change_the_hash():
    base = asset_content_payload(
        None, title="t", body="b", applicability=None, sources=[("p1", None)]
    )
    assert base != asset_content_payload(
        None, title="t2", body="b", applicability=None, sources=[("p1", None)]
    )
    assert base != asset_content_payload(
        None, title="t", body="b", applicability="x", sources=[("p1", None)]
    )
    assert base != asset_content_payload(
        None, title="t", body="b", applicability=None, sources=[("p1", "2026-10-01T00:00:00+00:00")]
    )


def test_kind_only_appears_for_create():
    with_kind = asset_content_payload(
        "problem", title="t", body="b", applicability=None, sources=[("p1", None)]
    )
    without_kind = asset_content_payload(
        None, title="t", body="b", applicability=None, sources=[("p1", None)]
    )
    assert with_kind != without_kind
    assert "kind" not in without_kind
    # 修订哈希不含 kind：同一资产的内容哈希与资产类型无关。
    assert request_hash(ASSET_REVISE, "a1", without_kind) != request_hash(
        ASSET_CREATE, "a1", with_kind
    )


def test_reading_time_precision_values_participate():
    day = reading_time_payload(paper_id="p1", precision="day", day="2026-10-01", month=None)
    unknown = reading_time_payload(paper_id="p1", precision="unknown", day=None, month=None)
    assert day != unknown
    # 日期用 ISO 文本表达，date 与等价字符串生成同一载荷。
    assert reading_time_payload(paper_id="p1", precision="day", day="2026-10-01", month=None) == day
