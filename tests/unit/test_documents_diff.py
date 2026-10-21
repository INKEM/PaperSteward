from __future__ import annotations

from pathlib import Path

from papersteward.documents.diff import (
    diff_profiles,
    diff_taxonomies,
    diff_values,
    render_diff,
)
from papersteward.documents.profile import parse_profile
from papersteward.documents.taxonomy import parse_taxonomy

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _profile(name: str):
    path = FIXTURES / "profiles" / name
    return parse_profile(path.read_text(encoding="utf-8"), source=str(path))


def _taxonomy(name: str):
    path = FIXTURES / "taxonomies" / name
    return parse_taxonomy(path.read_text(encoding="utf-8"), source=str(path))


def test_profile_diff_compares_entities_not_markdown() -> None:
    entries = {
        entry.path: entry for entry in diff_profiles(_profile("basic.md"), _profile("v2.md"))
    }

    assert entries["version"].change == "change"
    assert entries["core_questions.Q2.text"].change == "change"
    assert entries["subdirections.S1.target_share"].old == 0.35
    assert entries["subdirections.S1.target_share"].new == 0.4
    assert entries["include"].change == "add"
    assert entries["include"].new == "benchmarks with cross-segment reasoning"
    assert all("研究定位" not in entry.path for entry in entries.values())


def test_taxonomy_diff_is_keyed_by_tag_code() -> None:
    entries = {
        entry.path: entry for entry in diff_taxonomies(_taxonomy("basic.md"), _taxonomy("v2.md"))
    }

    assert entries["version"].change == "change"
    assert entries["tags.T01.definition"].change == "change"
    assert entries["tags.S03"].change == "add"
    assert entries["tags.S01.aliases"].new == "lexical retrieval"
    assert entries["tags.T05.definition"].change == "change"


def test_identical_documents_have_no_diff() -> None:
    profile = _profile("basic.md")

    assert diff_profiles(profile, profile) == []
    assert render_diff([]) == "无差异"


def test_scalar_list_order_is_not_a_diff() -> None:
    assert diff_values({"tags": ["a", "b"]}, {"tags": ["b", "a"]}) == []


def test_diff_values_reports_add_remove_change_and_renders() -> None:
    entries = diff_values(
        {"a": 1, "items": [{"id": "1", "v": 1}], "list": ["x"]},
        {"a": 2, "items": [{"id": "1", "v": 2}, {"id": "2", "v": 1}], "list": ["x", "y"]},
    )
    by_path = {entry.path: entry for entry in entries}

    assert by_path["a"].change == "change"
    assert by_path["items.1.v"].change == "change"
    assert by_path["items.2"].change == "add"
    assert by_path["list"].change == "add"
    assert by_path["list"].new == "y"

    rendered = render_diff(entries)
    assert "~ a: 1 -> 2" in rendered
    assert "+ items.2:" in rendered
    assert '+ list: "y"' in rendered


def test_diff_removes_keyed_items_and_renders_scalars() -> None:
    entries = diff_values(
        {"enabled": True, "items": [{"id": "1"}], "note": None},
        {"enabled": False, "items": [], "note": None},
    )
    by_path = {entry.path: entry for entry in entries}

    assert by_path["enabled"].change == "change"
    assert by_path["items.1"].change == "remove"

    rendered = render_diff(entries)
    assert "~ enabled: true -> false" in rendered
    assert "- items.1:" in rendered
    assert "null" not in rendered


def test_diff_indexes_unkeyed_object_lists() -> None:
    entries = diff_values(
        {"items": [{"a": 1}]},
        {"items": [{"a": 1}, {"a": 2}]},
    )

    assert len(entries) == 1
    assert entries[0].path == "items[1]"
    assert entries[0].change == "add"
