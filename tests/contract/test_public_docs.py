"""Public documentation must be usable from a fresh checkout."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[2]


def test_public_head_excludes_local_development_documents() -> None:
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    forbidden = ("docs/phases/", "docs/adr/", "dev-docs/")
    assert not [name for name in tracked if name.startswith(forbidden)]
    assert "AGENTS.md" not in tracked
    assert not [name for name in tracked if name.startswith("docs/PaperSteward_")]
    assert "dev-docs/" in (ROOT / ".gitignore").read_text()
    assert "dev-docs" in (ROOT / ".dockerignore").read_text().splitlines()


def test_readme_local_links_and_mit_license() -> None:
    readme = (ROOT / "README.md").read_text()
    assert not re.search(r"P[0-9]-S|docs/phases|docs/adr|dev-docs/|AGENTS\.md", readme)
    for target in re.findall(r"\]\(([^)]+)\)", readme):
        url = urlsplit(target)
        if not url.scheme and url.path:
            assert (ROOT / unquote(url.path)).is_file(), target
    license_text = (ROOT / "LICENSE").read_text()
    assert "MIT License" in license_text
    assert "Permission is hereby granted" in license_text
    assert "THE SOFTWARE IS PROVIDED" in license_text


def test_all_user_guide_links_and_no_development_links() -> None:
    documents = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
    for document in documents:
        source = document.read_text()
        assert not re.search(
            r"docs/phases|docs/adr|dev-docs/|AGENTS\.md|ADR00[0-9]+|P[0-9]-S[0-9]", source
        ), document
        for target in re.findall(r"\]\(([^)]+)\)", source):
            url = urlsplit(target)
            if not url.scheme and url.path:
                assert (document.parent / unquote(url.path)).is_file(), (document, target)
    assert (ROOT / "docs/images/today.jpg").stat().st_size > 1000
