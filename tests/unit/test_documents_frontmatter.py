from __future__ import annotations

import pytest

from papersteward.documents.frontmatter import (
    DocumentError,
    field_line,
    load_yaml_mapping,
    split_front_matter,
)


def test_unclosed_front_matter_is_rejected() -> None:
    with pytest.raises(DocumentError, match="结束分隔符"):
        split_front_matter("---\nversion: '1.0'\n# body\n", source="bad.md")


def test_empty_front_matter_is_rejected() -> None:
    front = split_front_matter("---\n---\n# body\n", source="bad.md")

    with pytest.raises(DocumentError, match="不能为空"):
        load_yaml_mapping(front, source="bad.md")


def test_front_matter_root_must_be_mapping() -> None:
    front = split_front_matter("---\n- one\n- two\n---\n# body\n", source="bad.md")

    with pytest.raises(DocumentError, match="mapping"):
        load_yaml_mapping(front, source="bad.md")


def test_field_line_finds_top_level_key_and_returns_none() -> None:
    front = split_front_matter(
        "---\nversion: '1.0'\nreading:\n  x: 1\n---\n# body\n", source="ok.md"
    )

    assert field_line(front, "version") == 2
    assert field_line(front, "reading") == 3
    assert field_line(front, "missing") is None


def test_document_error_str_contains_source_line_and_field() -> None:
    error = DocumentError("bad", source="x.md", field="front_matter.version", line=3)

    assert str(error) == "x.md:3: front_matter.version: bad"
    assert error.field == "front_matter.version"
    assert error.source == "x.md"
    assert error.line == 3
