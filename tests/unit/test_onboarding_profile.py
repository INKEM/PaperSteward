from pathlib import Path

import pytest
import yaml

from papersteward.core.errors import DomainValidationError
from papersteward.onboarding.profile import MAX_PROFILE_BYTES, parse_draft, profile_review

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "profiles"


def test_complete_draft_has_ready_review_and_both_diffs() -> None:
    base = parse_draft((FIXTURES / "basic.md").read_text(), "demo-profile")
    edited = parse_draft((FIXTURES / "v2.md").read_text(), "demo-profile")
    result = profile_review(edited, base)
    assert result["ready"]
    assert result["missing_fields"] == []
    assert "core_questions.Q2.text" in result["diff"]
    assert "-version:" in result["markdown_diff"]
    assert len(result["content_hash"]) == 64


def test_incomplete_draft_lists_missing_fields() -> None:
    text = (FIXTURES / "basic.md").read_text()
    front, body = text[4:].split("\n---\n", 1)
    data = yaml.safe_load(front)
    data["exclude"] = []
    data["reading"]["reading_goals"] = [" "]
    for direction in data["subdirections"]:
        direction["keywords"] = []
    draft = parse_draft("---\n" + yaml.safe_dump(data) + "---\n" + body, "demo-profile")
    result = profile_review(draft)
    assert not result["ready"]
    assert len(result["missing_fields"]) == 6


@pytest.mark.parametrize(
    "content,field",
    [
        ("x" * (MAX_PROFILE_BYTES + 1), "content_md"),
        ("中" * (MAX_PROFILE_BYTES // 3 + 1), "content_md"),
        ("---\nversion: 1\nitem: &alias [a]\ncopy: *alias\n---\n草案", "content_md"),
    ],
)
def test_rejects_oversized_or_expandable_yaml(content: str, field: str) -> None:
    with pytest.raises(DomainValidationError, match=field):
        parse_draft(content, "demo-profile")


def test_identity_must_match_request_and_version_is_bounded() -> None:
    text = (FIXTURES / "basic.md").read_text()
    with pytest.raises(DomainValidationError, match="profile_id"):
        parse_draft(text, "another-profile")
    with pytest.raises(DomainValidationError, match="version"):
        parse_draft(text.replace('version: "1.0"', 'version: "' + "v" * 33 + '"'), "demo-profile")
