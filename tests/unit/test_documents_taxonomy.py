from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from papersteward.core.enums import DocumentStatus, TagLevel
from papersteward.documents.frontmatter import DocumentError
from papersteward.documents.taxonomy import parse_taxonomy

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _section(code: str, name: str = "主题", *, definition: str = "定义。") -> str:
    return (
        f"## {code} {name}\n\n"
        f"- 定义：{definition}\n"
        "- 纳入：纳入条件。\n"
        "- 排除：排除条件。\n"
        "- 正例：[种子 A]、[种子 B]\n"
    )


def _taxonomy_text(
    sections: list[str], *, secondary_tags: list[dict[str, object]] | None = None
) -> str:
    data: dict[str, object] = {"version": "1.0", "taxonomy_id": "test-taxonomy"}
    if secondary_tags is not None:
        data["secondary_tags"] = secondary_tags
    front = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    return "---\n" + front + "---\n# 分类法\n\n" + "\n".join(sections)


def test_parse_taxonomy_fixture_round_trip() -> None:
    path = FIXTURES / "taxonomies" / "basic.md"
    taxonomy = parse_taxonomy(path.read_text(encoding="utf-8"), source=str(path))

    assert taxonomy.id == "demo-taxonomy"
    assert taxonomy.version == "1.0"
    assert taxonomy.status is DocumentStatus.DRAFT
    primary = [tag for tag in taxonomy.tags if tag.level is TagLevel.PRIMARY]
    secondary = [tag for tag in taxonomy.tags if tag.level is TagLevel.SECONDARY]
    assert len(primary) == 5
    assert [tag.code for tag in secondary] == ["S01", "S02"]

    t01 = next(tag for tag in primary if tag.code == "T01")
    assert t01.include == ("以检索或重排为核心贡献。",)
    assert t01.examples == ("种子 A", "种子 B")
    assert t01.parent_code is None

    s01 = next(tag for tag in secondary if tag.code == "S01")
    assert s01.parent_code == "T01"
    assert s01.aliases == ("sparse retrieval",)


def test_taxonomy_requires_five_primary_tags() -> None:
    with pytest.raises(DocumentError, match="5–9"):
        parse_taxonomy(_taxonomy_text([_section("T01")]), source="memory.md")


def test_missing_definition_is_line_located() -> None:
    path = FIXTURES / "taxonomies" / "invalid.md"
    with pytest.raises(DocumentError) as exc_info:
        parse_taxonomy(path.read_text(encoding="utf-8"), source=str(path))

    error = exc_info.value
    assert error.field == "taxonomy.T01.definition"
    assert error.line is not None
    assert "T01" in str(error)


def test_duplicate_primary_codes_are_rejected() -> None:
    sections = [_section("T01"), _section("T01", "重复"), _section("T02"), _section("T03")]
    with pytest.raises(DocumentError, match="主标签代码不能重复"):
        parse_taxonomy(_taxonomy_text(sections), source="memory.md")


def test_catch_all_tag_name_is_rejected() -> None:
    path = FIXTURES / "taxonomies" / "basic.md"
    text = path.read_text(encoding="utf-8").replace("## T05 文档智能应用", "## T05 其他")
    with pytest.raises(DocumentError, match="兜底主标签"):
        parse_taxonomy(text, source=str(path))


def test_secondary_parent_must_exist() -> None:
    sections = [_section(f"T{i:02d}") for i in range(1, 6)]
    text = _taxonomy_text(
        sections,
        secondary_tags=[
            {
                "code": "S01",
                "name": "副标签",
                "parent_code": "T99",
            }
        ],
    )

    with pytest.raises(DocumentError, match="parent_code 不存在"):
        parse_taxonomy(text, source="memory.md")


def test_duplicate_secondary_code_is_rejected() -> None:
    sections = [_section(f"T{i:02d}") for i in range(1, 6)]
    text = _taxonomy_text(
        sections,
        secondary_tags=[
            {"code": "S01", "name": "副标签 1"},
            {"code": "S01", "name": "副标签 2"},
        ],
    )

    with pytest.raises(DocumentError, match="标签代码重复"):
        parse_taxonomy(text, source="memory.md")


def test_missing_front_matter_is_rejected() -> None:
    with pytest.raises(DocumentError, match="front matter"):
        parse_taxonomy("# 只有正文\n", source="memory.md")


@pytest.mark.parametrize("version", [None, True, [], ""])
def test_invalid_taxonomy_version_is_field_located(version: object) -> None:
    text = (
        "---\n"
        f"version: {yaml.safe_dump(version, allow_unicode=True).strip()}\n"
        "---\n# 分类法\n" + _section("T01")
    )
    with pytest.raises(DocumentError) as exc_info:
        parse_taxonomy(text, source="memory.md")

    assert exc_info.value.field == "front_matter.version"


def test_numeric_version_and_document_id_override() -> None:
    text = "---\nversion: 1.0\n---\n# 分类法\n\n" + "\n".join(
        _section(f"T{i:02d}") for i in range(1, 6)
    )
    taxonomy = parse_taxonomy(text, source="/tmp/custom-taxonomy.md", document_id="explicit")
    assert taxonomy.version == "1.0"
    assert taxonomy.id == "explicit"

    fallback = parse_taxonomy(text, source="<memory>")
    assert fallback.id == "taxonomy"


def test_taxonomy_without_sections_is_rejected() -> None:
    with pytest.raises(DocumentError, match="至少一个"):
        parse_taxonomy(_taxonomy_text([]), source="memory.md")


@pytest.mark.parametrize(
    ("missing_line", "message", "field"),
    [
        ("- 纳入：纳入条件。", "缺少纳入条件", "taxonomy.T01.include"),
        ("- 排除：排除条件。", "缺少排除条件", "taxonomy.T01.exclude"),
        ("- 正例：[种子 A]、[种子 B]", "缺少正例", "taxonomy.T01.examples"),
    ],
)
def test_missing_required_section_fields(missing_line: str, message: str, field: str) -> None:
    section = _section("T01").replace(missing_line + "\n", "")
    with pytest.raises(DocumentError) as exc_info:
        parse_taxonomy(_taxonomy_text([section]), source="memory.md")

    assert exc_info.value.field == field
    assert message in str(exc_info.value)


def test_malformed_tag_heading_is_rejected() -> None:
    path = FIXTURES / "taxonomies" / "basic.md"
    text = path.read_text(encoding="utf-8") + "\n## T06\n"

    with pytest.raises(DocumentError) as exc_info:
        parse_taxonomy(text, source=str(path))

    error = exc_info.value
    assert error.field == "taxonomy.heading"
    assert error.line is not None
    assert "缺少名称" in error.message
