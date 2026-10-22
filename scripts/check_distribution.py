"""Validate release archives without importing application code or accessing the network."""

from __future__ import annotations

import sys
import tarfile
from pathlib import Path
from zipfile import ZipFile


def check(path: Path) -> None:
    wheel = path.suffix == ".whl"
    if wheel:
        with ZipFile(path) as archive:
            files = {
                name: archive.read(name) for name in archive.namelist() if not name.endswith("/")
            }
        prefix = "papersteward/_migrations/"
    else:
        with tarfile.open(path) as archive:
            files = {}
            for member in archive.getmembers():
                if member.isfile():
                    content = archive.extractfile(member)
                    assert content is not None
                    files[member.name.split("/", 1)[1]] = content.read()
        prefix = "migrations/"
    for name in files:
        parts = Path(name).parts
        assert not {"docs", "dev-docs"}.intersection(parts), name
        assert Path(name).name not in {"AGENTS.md", ".env"}, name
    for source in Path("migrations/versions").glob("*.py"):
        assert files[prefix + "versions/" + source.name] == source.read_bytes(), source
    assert prefix + "env.py" in files
    assert any(name.endswith("LICENSE") for name in files)
    assert any(name.endswith("HTMX-LICENSE") for name in files)
    if wheel:
        assert prefix + "alembic.ini" in files
    else:
        assert "setup.py" in files and "alembic.ini" in files
    print(f"{path.name}: distribution checks passed")


if __name__ == "__main__":
    for argument in sys.argv[1:]:
        check(Path(argument))
