from pathlib import Path

import pytest

from papersteward.core.errors import DomainValidationError
from papersteward.onboarding.taxonomy import parse_taxonomy_draft

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "taxonomies"
TEXT = (FIXTURES / "onboarding.md").read_text(encoding="utf-8")


def test_valid_taxonomy_draft_parses_primary_tags() -> None:
    taxonomy = parse_taxonomy_draft(TEXT, "onboarding-taxonomy")
    assert taxonomy.id == "onboarding-taxonomy"
    primary = [tag for tag in taxonomy.tags if tag.level.value == "primary"]
    assert len(primary) == 5
    assert all(tag.include and tag.exclude and tag.examples for tag in primary)


@pytest.mark.parametrize(
    "text",
    [
        "中" * 50000,
        "---\na: &value [1]\nb: *value\n---\n草案",
        "---\nversion: 1\n---\n没有任何主标签",
    ],
)
def test_document_input_is_bounded_and_does_not_expand_yaml(text: str) -> None:
    with pytest.raises(DomainValidationError):
        parse_taxonomy_draft(text, "onboarding-taxonomy")
