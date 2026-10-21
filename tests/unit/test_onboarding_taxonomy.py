from dataclasses import replace
from pathlib import Path

import pytest

from papersteward.core.errors import DomainValidationError
from papersteward.onboarding.taxonomy import SeedLabel, parse_taxonomy_draft, validate_taxonomy

TEXT = (Path(__file__).resolve().parents[1] / "fixtures/taxonomies/onboarding.md").read_text()


def seeds() -> list[SeedLabel]:
    return [
        SeedLabel(
            paper_id=f"syn-T{tag:02}-{index}", title=f"合成 {tag}/{index}", primary_tag=f"T{tag:02}"
        )
        for tag in range(1, 6)
        for index in range(1, 5)
    ]


def test_complete_taxonomy_report_is_deterministic() -> None:
    taxonomy = parse_taxonomy_draft(TEXT, "onboarding-taxonomy")
    report = validate_taxonomy(taxonomy, seeds())
    assert report["g2_ready"] and report["issues"] == []
    assert report["coverage"] == 1 and report["max_tag_share"] == 0.2
    assert len(report["boundaries"]) == 10
    assert [item["count"] for item in report["distribution"]] == [4] * 5
    assert report == validate_taxonomy(taxonomy, seeds())


@pytest.mark.parametrize("uncovered,expected", [(2, True), (3, False)])
def test_coverage_counts_unclassified_seeds_in_denominator(uncovered: int, expected: bool) -> None:
    labels = seeds()
    for index in [3, 7, 11][:uncovered]:
        labels[index] = labels[index].model_copy(update={"primary_tag": None})
    report = validate_taxonomy(parse_taxonomy_draft(TEXT, "onboarding-taxonomy"), labels)
    assert report["coverage"] == (20 - uncovered) / 20
    assert report["g2_ready"] is expected
    assert len(report["unclassified"]) == uncovered


@pytest.mark.parametrize("count,allowed", [(10, True), (11, False)])
def test_single_tag_fifty_percent_limit_is_inclusive(count: int, allowed: bool) -> None:
    labels = seeds()
    labels = [
        label.model_copy(update={"primary_tag": "T01" if index < count else "T02"})
        for index, label in enumerate(labels)
    ]
    report = validate_taxonomy(parse_taxonomy_draft(TEXT, "onboarding-taxonomy"), labels)
    assert report["max_tag_share"] == count / 20
    assert (not any(issue["field"] == "taxonomy.balance" for issue in report["issues"])) is allowed


@pytest.mark.parametrize(
    "kind", ["empty", "unknown", "nine", "boundary", "duplicate_boundary", "examples"]
)
def test_incomplete_taxonomy_never_silently_passes(kind: str) -> None:
    taxonomy = parse_taxonomy_draft(TEXT, "onboarding-taxonomy")
    labels = seeds()
    if kind == "empty":
        labels = []
    elif kind == "unknown":
        labels[-1] = labels[-1].model_copy(update={"primary_tag": "T99"})
    elif kind == "nine":
        taxonomy = replace(
            taxonomy,
            tags=taxonomy.tags
            + tuple(replace(taxonomy.tags[0], code=f"T{index:02}") for index in range(6, 10)),
        )
    elif kind in {"boundary", "duplicate_boundary"}:
        lines = taxonomy.content_md.splitlines()
        indices = [i for i, line in enumerate(lines) if line.startswith("- 边界：")]
        lines[indices[1]] = "- 边界：缺少判定与理由" if kind == "boundary" else lines[indices[0]]
        taxonomy = replace(taxonomy, content_md="\n".join(lines))
    else:
        taxonomy = replace(
            taxonomy, tags=(replace(taxonomy.tags[0], examples=("missing",)), *taxonomy.tags[1:])
        )
    assert not validate_taxonomy(taxonomy, labels)["g2_ready"]


def test_duplicate_seed_ids_are_rejected() -> None:
    labels = seeds()
    with pytest.raises(DomainValidationError, match="paper_id"):
        validate_taxonomy(parse_taxonomy_draft(TEXT, "onboarding-taxonomy"), labels + labels[:1])


@pytest.mark.parametrize("text", ["中" * 50000, "---\na: &value [1]\nb: *value\n---\n草案"])
def test_document_input_is_bounded_and_does_not_expand_yaml(text: str) -> None:
    with pytest.raises(DomainValidationError, match="content_md"):
        parse_taxonomy_draft(text, "onboarding-taxonomy")
