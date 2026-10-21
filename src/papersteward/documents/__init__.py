"""profile / taxonomy 文档解析、版本、diff 与确认服务。"""

from __future__ import annotations

from papersteward.documents.diff import (
    DiffEntry,
    diff_profiles,
    diff_taxonomies,
    diff_values,
    render_diff,
)
from papersteward.documents.frontmatter import DocumentError
from papersteward.documents.profile import parse_profile
from papersteward.documents.service import (
    archive_profile,
    archive_taxonomy,
    confirm_profile,
    confirm_taxonomy,
    import_profile,
    import_taxonomy,
)
from papersteward.documents.taxonomy import parse_taxonomy

__all__ = [
    "DiffEntry",
    "DocumentError",
    "archive_profile",
    "archive_taxonomy",
    "confirm_profile",
    "confirm_taxonomy",
    "diff_profiles",
    "diff_taxonomies",
    "diff_values",
    "import_profile",
    "import_taxonomy",
    "parse_profile",
    "parse_taxonomy",
    "render_diff",
]
