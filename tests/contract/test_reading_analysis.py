"""阅读分析契约冻结：输入严格性、类别词表与授权版本常量（R2-02）。"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from papersteward.reading.insights.contracts import (
    ANALYSIS_CONSENT_VERSION,
    AnalysisGrantInput,
    ExcerptInput,
    ManifestSelectionInput,
    MaterialExclusion,
    MaterialRef,
)


def test_analysis_consent_version_is_frozen():
    assert ANALYSIS_CONSENT_VERSION == "reading-analysis.consent/1"


def _ref(**overrides: object) -> MaterialRef:
    payload: dict[str, object] = {
        "kind": "user_notes",
        "target_id": "p1",
        "paper_id": "p1",
        "version_key": "2026-10-20T02:00:00+00:00",
        "fields": ["note"],
        "ref_hash": "a" * 64,
    }
    payload.update(overrides)
    return MaterialRef.model_validate(payload)


def test_material_ref_rejects_extra_fields_and_bad_hash():
    with pytest.raises(ValidationError):
        _ref(surprise=1)
    with pytest.raises(ValidationError):
        _ref(ref_hash="short")
    with pytest.raises(ValidationError):
        _ref(kind="not_a_kind")
    assert _ref().ref_hash == "a" * 64


def test_grant_input_requires_acknowledged_and_classes():
    good = AnalysisGrantInput.model_validate(
        {"scope_mode": "workspace", "material_classes": ["user_notes"], "acknowledged": True}
    )
    assert good.scope_mode == "workspace"
    for payload in [
        {"scope_mode": "workspace", "material_classes": ["user_notes"]},
        {"scope_mode": "workspace", "material_classes": [], "acknowledged": True},
        {"scope_mode": "workspace", "material_classes": ["user_notes"], "acknowledged": False},
        {"scope_mode": "once", "material_classes": ["user_notes"], "acknowledged": True, "x": 1},
        {"scope_mode": "every_time", "material_classes": ["user_notes"], "acknowledged": True},
    ]:
        with pytest.raises(ValidationError):
            AnalysisGrantInput.model_validate(payload)


def test_excerpt_input_bounds():
    assert ExcerptInput.model_validate({"content": "摘录"}).content == "摘录"
    for payload in [
        {"content": ""},
        {"content": "x" * 8001},
        {"content": "x", "source_note": "y" * 501},
        {"content": "x", "extra": 1},
    ]:
        with pytest.raises(ValidationError):
            ExcerptInput.model_validate(payload)


def test_selection_input_requires_papers_and_kinds():
    good = ManifestSelectionInput.model_validate(
        {
            "paper_ids": ["p1"],
            "kinds": ["archive_metadata", "user_notes"],
            "exclusions": [{"paper_id": "p2", "kind": None, "reason": "任务无关"}],
        }
    )
    assert good.exclusions[0].kind is None
    for payload in [
        {"paper_ids": [], "kinds": ["archive_metadata"]},
        {"paper_ids": ["p1"], "kinds": []},
        {"paper_ids": ["p1"], "kinds": ["archive_metadata"], "exclusions": [{"paper_id": "p2"}]},
    ]:
        with pytest.raises(ValidationError):
            ManifestSelectionInput.model_validate(payload)


def test_material_exclusion_reason_required():
    assert MaterialExclusion.model_validate({"paper_id": "p2", "reason": "不相关"}).kind is None
    with pytest.raises(ValidationError):
        MaterialExclusion.model_validate({"paper_id": "p2", "reason": ""})


def test_manifest_content_rejects_unknown_fields():
    from papersteward.reading.insights.contracts import AnalysisManifestContent

    with pytest.raises(ValidationError):
        AnalysisManifestContent.model_validate(
            {
                "purpose": "reading_reflection",
                "built_at": datetime(2026, 10, 26, tzinfo=UTC),
                "materials": [],
                "coverage": {"archived_total": 0, "selected_papers": 0},
                "surprise": 1,
            }
        )


def test_flow_request_insight_analysis_coherence():
    from papersteward.jobs.ui_flow import FlowRequest

    good = FlowRequest.model_validate(
        {
            "run_id": "run-1",
            "kind": "insight_analysis",
            "analysis": {
                "manifest_id": "m1",
                "manifest_hash": "a" * 64,
                "grant_id": "g1",
            },
        }
    )
    assert good.analysis is not None and good.analysis.purpose == "reading_reflection"
    ok_review = FlowRequest.model_validate(
        {
            "run_id": "run-2",
            "kind": "review",
            "review": {"period": "week", "anchor_date": "2026-10-22"},
        }
    )
    assert ok_review.analysis is None
    for payload in [
        {"run_id": "r", "kind": "insight_analysis"},
        {
            "run_id": "r",
            "kind": "insight_analysis",
            "analysis": {"manifest_id": "m", "manifest_hash": "a" * 64, "grant_id": "g", "x": 1},
        },
        {
            "run_id": "r",
            "kind": "insight_analysis",
            "analysis": {"manifest_id": "m", "manifest_hash": "short", "grant_id": "g"},
        },
        {
            "run_id": "r",
            "kind": "insight_analysis",
            "analysis": {
                "manifest_id": "m",
                "manifest_hash": "a" * 64,
                "grant_id": "g",
            },
            "focus": "关注点",
        },
        {
            "run_id": "r",
            "kind": "sync",
            "analysis": {"manifest_id": "m", "manifest_hash": "a" * 64, "grant_id": "g"},
        },
    ]:
        with pytest.raises(ValidationError):
            FlowRequest.model_validate(payload)
