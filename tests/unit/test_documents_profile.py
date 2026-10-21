from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from papersteward.core.enums import DocumentStatus
from papersteward.documents.frontmatter import DocumentError
from papersteward.documents.profile import parse_profile

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
BODY = "# 研究方向档案\n\n这是正文。\n"


def _front_matter(**overrides: Any) -> str:
    data: dict[str, Any] = {
        "version": "1.0",
        "profile_id": "test-profile",
        "core_questions": [
            {"id": "Q1", "text": "问题一"},
            {"id": "Q2", "text": "问题二"},
            {"id": "Q3", "text": "问题三"},
        ],
        "subdirections": [
            {"id": "S1", "name": "方向一", "priority": "high", "target_share": 0.5},
            {"id": "S2", "name": "方向二", "priority": "medium", "target_share": 0.5},
        ],
        "include": ["可复现的方法论文"],
        "exclude": ["无评测的 prompt 技巧"],
        "reading": {"weekly_capacity": 10, "deep_skim_ratio": [0.3, 0.7]},
    }
    data.update(overrides)
    return "---\n" + yaml.safe_dump(data, allow_unicode=True, sort_keys=False) + "---\n" + BODY


def test_parse_profile_fixture_round_trip() -> None:
    path = FIXTURES / "profiles" / "basic.md"
    profile = parse_profile(path.read_text(encoding="utf-8"), source=str(path))

    assert profile.id == "demo-profile"
    assert profile.version == "1.0"
    assert profile.status is DocumentStatus.DRAFT
    assert profile.confirmed_at is None
    assert profile.content_md.startswith("---")
    assert profile.parsed_json["reading"]["weekly_capacity"] == 10
    assert profile.parsed_json["positioning"]["domain"] == "信息检索 / 自然语言处理"
    assert len(profile.change_log) == 1
    assert "initial profile" in profile.change_log[0]


def test_profile_id_can_be_overridden_and_unknown_fields_are_kept() -> None:
    profile = parse_profile(
        _front_matter(custom_field={"nested": [1, 2, 3]}),
        source="memory.md",
        document_id="explicit-id",
    )

    assert profile.id == "explicit-id"
    assert profile.parsed_json["custom_field"] == {"nested": [1, 2, 3]}


def test_numeric_version_is_normalized_to_string() -> None:
    profile = parse_profile(_front_matter(version=1.0), source="memory.md")

    assert profile.version == "1.0"


def test_missing_front_matter_is_field_located() -> None:
    with pytest.raises(DocumentError) as exc_info:
        parse_profile("# 没有 front matter\n", source="bad.md")

    error = exc_info.value
    assert error.source == "bad.md"
    assert error.line == 1
    assert error.field == "document"


def test_invalid_yaml_is_line_located() -> None:
    with pytest.raises(DocumentError) as exc_info:
        parse_profile("---\nversion: [1.0\n---\n# body\n", source="bad.md")

    error = exc_info.value
    assert error.line is not None and error.line >= 2
    assert error.field == "front_matter"
    assert "YAML" in error.message


def test_too_few_core_questions_are_rejected() -> None:
    with pytest.raises(DocumentError) as exc_info:
        parse_profile(_front_matter(core_questions=[{"id": "Q1", "text": "only"}]))

    error = exc_info.value
    assert error.field == "front_matter.core_questions"
    assert error.line is not None


def test_target_share_must_sum_to_one() -> None:
    with pytest.raises(DocumentError, match="target_share 之和必须为 1.0"):
        parse_profile(
            _front_matter(
                subdirections=[
                    {"id": "S1", "name": "方向一", "target_share": 0.4},
                    {"id": "S2", "name": "方向二", "target_share": 0.4},
                ]
            ),
            source="memory.md",
        )


def test_duplicate_question_ids_are_rejected() -> None:
    with pytest.raises(DocumentError, match="id 不能重复"):
        parse_profile(
            _front_matter(
                core_questions=[
                    {"id": "Q1", "text": "问题一"},
                    {"id": "Q1", "text": "问题二"},
                    {"id": "Q3", "text": "问题三"},
                ]
            ),
            source="memory.md",
        )


def test_deep_skim_ratio_must_sum_to_one() -> None:
    with pytest.raises(DocumentError, match="deep_skim_ratio 之和必须为 1.0"):
        parse_profile(
            _front_matter(reading={"weekly_capacity": 10, "deep_skim_ratio": [0.2, 0.3]}),
            source="memory.md",
        )


def test_missing_body_is_rejected() -> None:
    text = "---\nversion: '1.0'\n---\n\n"
    with pytest.raises(DocumentError, match="Markdown 正文"):
        parse_profile(text, source="memory.md")


@pytest.mark.parametrize("version", [None, True, [], ""])
def test_invalid_version_is_field_located(version: object) -> None:
    with pytest.raises(DocumentError) as exc_info:
        parse_profile(_front_matter(version=version), source="memory.md")

    assert exc_info.value.field == "front_matter.version"


def test_ratio_out_of_range_and_empty_include_are_rejected() -> None:
    with pytest.raises(DocumentError) as exc_info:
        parse_profile(
            _front_matter(reading={"weekly_capacity": 10, "deep_skim_ratio": [1.2, -0.2]}),
            source="memory.md",
        )
    assert exc_info.value.field == "front_matter.reading.deep_skim_ratio"

    with pytest.raises(DocumentError, match="include 不能包含空条目"):
        parse_profile(_front_matter(include=[" "]), source="memory.md")

    with pytest.raises(DocumentError, match="exclude 不能包含空条目"):
        parse_profile(_front_matter(exclude=[" "]), source="memory.md")


def test_profile_id_falls_back_to_filename_and_string_change_log() -> None:
    text = _front_matter(profile_id=None, change_log=["plain change"])
    profile = parse_profile(text, source="/tmp/custom-profile.md")

    assert profile.id == "custom-profile"
    assert profile.change_log == ("plain change",)

    fallback = parse_profile(_front_matter(profile_id=None), source="<memory>")
    assert fallback.id == "profile"
