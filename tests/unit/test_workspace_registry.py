"""工作区文件注册表的纯逻辑测试：清单读写、名称校验、对账与默认工作区保护。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from papersteward.errors import ConflictError, InvalidRequestError, NotFoundError
from papersteward.storage.workspace_registry import (
    DEFAULT_WORKSPACE_ID,
    WorkspaceRegistry,
    validate_workspace_name,
)

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


@pytest.fixture
def registry(tmp_path: Path) -> WorkspaceRegistry:
    return WorkspaceRegistry(tmp_path)


def test_default_workspace_always_listed_first(registry: WorkspaceRegistry, tmp_path: Path) -> None:
    infos = registry.list_all()
    assert [info.id for info in infos] == [DEFAULT_WORKSPACE_ID]
    assert infos[0].directory == tmp_path
    assert registry.get(DEFAULT_WORKSPACE_ID).directory == tmp_path


def test_create_writes_index_and_directory(registry: WorkspaceRegistry, tmp_path: Path) -> None:
    info = registry.create("博弈论综述", now=NOW)
    assert info.name == "博弈论综述"
    assert info.directory.parent == tmp_path / "workspaces"
    assert info.directory.is_dir()
    payload = json.loads((tmp_path / "workspaces" / "index.json").read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert [entry["name"] for entry in payload["workspaces"]] == ["博弈论综述"]
    assert [entry["id"] for entry in payload["workspaces"]] == [info.id]


def test_create_rejects_duplicate_name_case_insensitive(
    registry: WorkspaceRegistry,
) -> None:
    registry.create("Review 2026", now=NOW)
    with pytest.raises(ConflictError):
        registry.create("review 2026", now=NOW)


@pytest.mark.parametrize("name", ["", "   ", "a" * 41, None])
def test_create_rejects_invalid_name(registry: WorkspaceRegistry, name: object) -> None:
    with pytest.raises(InvalidRequestError):
        registry.create(name, now=NOW)  # type: ignore[arg-type]


def test_validate_workspace_name_collapses_whitespace() -> None:
    assert validate_workspace_name("  第二   方向 \n") == "第二 方向"


def test_get_unknown_workspace_raises(registry: WorkspaceRegistry) -> None:
    with pytest.raises(NotFoundError):
        registry.get("w-missing")


def test_delete_moves_directory_into_backups(registry: WorkspaceRegistry, tmp_path: Path) -> None:
    info = registry.create("待删除", now=NOW)
    (info.directory / "papersteward.db").write_bytes(b"sqlite")
    archived = registry.delete(info.id, now=NOW)
    assert archived.is_dir()
    assert archived.parent == tmp_path / "backups"
    assert archived.name.startswith("deleted-")
    assert (archived / "papersteward.db").read_bytes() == b"sqlite"
    assert not info.directory.exists()
    assert [entry.id for entry in registry.list_all()] == [DEFAULT_WORKSPACE_ID]


def test_delete_default_workspace_forbidden(registry: WorkspaceRegistry) -> None:
    with pytest.raises(InvalidRequestError):
        registry.delete(DEFAULT_WORKSPACE_ID, now=NOW)


def test_delete_unknown_workspace_raises(registry: WorkspaceRegistry) -> None:
    with pytest.raises(NotFoundError):
        registry.delete("w-missing", now=NOW)


def test_reconcile_adopts_orphan_directory_and_drops_missing(
    registry: WorkspaceRegistry, tmp_path: Path
) -> None:
    info = registry.create("正常", now=NOW)
    orphan = tmp_path / "workspaces" / "w-orphan01"
    orphan.mkdir()
    (tmp_path / "workspaces" / "index.json").write_text(
        json.dumps({"version": 1, "workspaces": []}, ensure_ascii=False), encoding="utf-8"
    )
    registry.reconcile()
    adopted = {entry.id: entry for entry in registry.list_all()}
    assert info.id in adopted
    assert "w-orphan01" in adopted
    assert adopted["w-orphan01"].name == "w-orphan01"

    # 条目在、目录没了：剔除。
    (tmp_path / "workspaces" / "w-orphan01").rmdir()
    registry.reconcile()
    assert "w-orphan01" not in {entry.id for entry in registry.list_all()}


def test_reconcile_ignores_non_directory_and_bad_names(
    registry: WorkspaceRegistry, tmp_path: Path
) -> None:
    (tmp_path / "workspaces" / "index.json").parent.mkdir(parents=True)
    (tmp_path / "workspaces" / "not-a-ws").mkdir()
    (tmp_path / "workspaces" / "../escape").mkdir()
    (tmp_path / "workspaces" / "afile").write_text("x", encoding="utf-8")
    registry.reconcile()
    assert [entry.id for entry in registry.list_all()] == [DEFAULT_WORKSPACE_ID]


def test_corrupt_index_self_heals(registry: WorkspaceRegistry, tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    root.mkdir()
    (root / "index.json").write_text("{not json", encoding="utf-8")
    assert registry.list_all() == [registry.default_info()]
    corrupt = list(root.glob("index.json.corrupt-*"))
    assert len(corrupt) == 1
