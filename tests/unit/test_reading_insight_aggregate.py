"""反馈变化清单的纯计算验证：初始不算修订、null 清除有效、重复写入不产事件。"""

from datetime import UTC, datetime

from papersteward.reading.insights.aggregate import feedback_changes
from papersteward.reading.insights.contracts import FeedbackVersion

T0 = datetime(2026, 10, 20, 2, tzinfo=UTC)
T1 = datetime(2026, 10, 22, 3, tzinfo=UTC)
T2 = datetime(2026, 10, 23, 3, tzinfo=UTC)
T3 = datetime(2026, 10, 24, 3, tzinfo=UTC)
T4 = datetime(2026, 10, 25, 3, tzinfo=UTC)


def version(at: datetime, score: int | None, note: str | None, *, initial: bool = False):
    return FeedbackVersion(recorded_at=at, score=score, note=note, is_initial=initial)


def test_initial_feedback_never_counts_as_revision():
    assert feedback_changes("p", [version(T0, 5, "初始", initial=True)]) == []
    assert feedback_changes("p", []) == []


def test_score_and_note_changes_are_detected_independently():
    versions = [
        version(T0, 5, "初始", initial=True),
        version(T1, 4, "初始"),
        version(T2, 4, "更新"),
    ]
    changes = feedback_changes("p", versions)
    assert [(c.kind, c.old, c.new, c.recorded_at) for c in changes] == [
        ("score", 5, 4, T1),
        ("note", "初始", "更新", T2),
    ]


def test_clearing_to_null_is_a_real_change():
    versions = [
        version(T0, 5, "初始", initial=True),
        version(T1, None, "初始"),
        version(T2, None, None),
    ]
    changes = feedback_changes("p", versions)
    assert [(c.kind, c.old, c.new) for c in changes] == [
        ("score", 5, None),
        ("note", "初始", None),
    ]


def test_unchanged_rewrite_creates_no_change_event():
    versions = [
        version(T0, 5, "初始", initial=True),
        version(T1, 5, "初始"),
        version(T2, 5, "初始"),
    ]
    assert feedback_changes("p", versions) == []


def test_same_timestamp_row_changing_both_fields_records_score_then_note():
    versions = [
        version(T0, 5, "初始", initial=True),
        version(T1, 2, "改写"),
    ]
    changes = feedback_changes("p", versions)
    assert [c.kind for c in changes] == ["score", "note"]
    assert all(c.recorded_at == T1 for c in changes)
    assert all(c.paper_id == "p" for c in changes)
