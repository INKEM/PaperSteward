"""多工作区注册表：一个工作区 = data_dir 下的独立目录 + 独立 SQLite 库。

- 默认工作区 id 固定为 ``default``，目录即 data_dir 根，向后兼容既有数据；
- 其余工作区落盘在 ``data_dir/workspaces/<id>/``，清单写在
  ``data_dir/workspaces/index.json``，只登记元数据，不含论文数据；
- 注册表自身零数据库依赖；删除默认工作区被禁止，等价语义由
  「重置工作区」（``storage/workspace.py``）覆盖。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from papersteward.errors import ConflictError, InvalidRequestError, NotFoundError

logger = logging.getLogger(__name__)

DEFAULT_WORKSPACE_ID = "default"
DEFAULT_WORKSPACE_NAME = "默认工作区"
INDEX_VERSION = 1
NAME_MAX = 40
# id 只作为目录名使用：必须带 w- 前缀（与 "default" 区分），白名单字符杜绝路径穿越。
_WORKSPACE_ID_RE = re.compile(r"^w-[a-z0-9][a-z0-9-]{0,62}$")


@dataclass(frozen=True)
class WorkspaceInfo:
    """工作区元数据；``directory`` 是该工作区的数据目录。"""

    id: str
    name: str
    created_at: str
    directory: Path


def validate_workspace_name(name: object) -> str:
    """规范化并校验用户输入的工作区名称。"""
    if not isinstance(name, str):
        raise InvalidRequestError("请填写工作区名称")
    normalized = " ".join(name.split())
    if not normalized or len(normalized) > NAME_MAX:
        raise InvalidRequestError(f"工作区名称需要 1–{NAME_MAX} 个字符")
    return normalized


class WorkspaceRegistry:
    """data_dir 下工作区清单的文件式注册表；线程安全，写操作原子落盘。"""

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._root = data_dir / "workspaces"
        self._index_path = self._root / "index.json"
        self._lock = threading.Lock()

    def default_info(self) -> WorkspaceInfo:
        return WorkspaceInfo(
            id=DEFAULT_WORKSPACE_ID,
            name=DEFAULT_WORKSPACE_NAME,
            created_at="",
            directory=self._data_dir,
        )

    def list_all(self) -> list[WorkspaceInfo]:
        """全部工作区；默认工作区固定排在首位。"""
        with self._lock:
            entries = self._read()
        return [self.default_info()] + [self._to_info(entry) for entry in entries]

    def get(self, workspace_id: str) -> WorkspaceInfo:
        """按 id 取单个工作区；不存在抛 NotFoundError。"""
        if workspace_id == DEFAULT_WORKSPACE_ID:
            return self.default_info()
        with self._lock:
            entries = self._read()
        for entry in entries:
            if entry["id"] == workspace_id:
                return self._to_info(entry)
        raise NotFoundError("工作区不存在或已被删除")

    def create(self, name: str, *, now: datetime) -> WorkspaceInfo:
        """新建工作区：先建目录再写清单，中间态由 reconcile 补录。"""
        normalized = validate_workspace_name(name)
        workspace_id = f"w-{uuid4().hex[:8]}"
        with self._lock:
            entries = self._read()
            if any(entry["name"].casefold() == normalized.casefold() for entry in entries):
                raise ConflictError("已有同名工作区，请换一个名称")
            self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory = self._root / workspace_id
            directory.mkdir(mode=0o700)
            entry = {"id": workspace_id, "name": normalized, "created_at": now.isoformat()}
            self._write([*entries, entry])
        return self._to_info(entry)

    def delete(self, workspace_id: str, *, now: datetime) -> Path:
        """删除非默认工作区：整目录移入 data_dir/backups/，返回归档路径。

        调用方须先确认该工作区没有运行中的任务并释放其运行时（引擎等）。
        """
        if workspace_id == DEFAULT_WORKSPACE_ID:
            raise InvalidRequestError("默认工作区不能删除；如需清空数据请用设置页的「重置工作区」")
        with self._lock:
            entries = self._read()
            remaining = [entry for entry in entries if entry["id"] != workspace_id]
            if len(remaining) == len(entries):
                raise NotFoundError("工作区不存在或已被删除")
            directory = self._root / workspace_id
            archived = directory
            if directory.is_dir():
                backups = self._data_dir / "backups"
                backups.mkdir(parents=True, exist_ok=True, mode=0o700)
                archived = backups / f"deleted-{now:%Y%m%d-%H%M%S}-{workspace_id}"
                shutil.move(str(directory), str(archived))
            self._write(remaining)
        return archived

    def reconcile(self) -> None:
        """启动对账：补录游离目录，剔除目录已失的条目。"""
        with self._lock:
            entries = self._read()
            changed = False
            known = {entry["id"] for entry in entries}
            if self._root.is_dir():
                for child in sorted(self._root.iterdir()):
                    if not child.is_dir() or not _WORKSPACE_ID_RE.fullmatch(child.name):
                        continue
                    if child.name in known:
                        continue
                    mtime = datetime.fromtimestamp(child.stat().st_mtime, tz=UTC)
                    entries.append(
                        {
                            "id": child.name,
                            "name": child.name,
                            "created_at": mtime.isoformat(),
                        }
                    )
                    known.add(child.name)
                    changed = True
            kept = [entry for entry in entries if (self._root / entry["id"]).is_dir()]
            if len(kept) != len(entries):
                entries = kept
                changed = True
            if changed:
                self._write(entries)

    def _to_info(self, entry: dict[str, str]) -> WorkspaceInfo:
        return WorkspaceInfo(
            id=entry["id"],
            name=entry["name"],
            created_at=entry["created_at"],
            directory=self._root / entry["id"],
        )

    # 以下读写都要求已持有 self._lock。

    def _read(self) -> list[dict[str, str]]:
        """读清单；文件缺失返回空，损坏时归档坏文件并按空清单继续。"""
        try:
            payload = json.loads(self._index_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            corrupt = self._index_path.with_name(
                f"index.json.corrupt-{datetime.now(UTC):%Y%m%d-%H%M%S}"
            )
            try:
                os.replace(self._index_path, corrupt)
            except OSError:
                logger.warning("工作区清单损坏且无法归档：%s", self._index_path)
            else:
                logger.warning("工作区清单损坏，已归档为 %s 并按空清单继续", corrupt)
            return []
        valid: list[dict[str, str]] = []
        for entry in payload.get("workspaces", []) if isinstance(payload, dict) else []:
            if not isinstance(entry, dict):
                continue
            workspace_id = entry.get("id")
            name = entry.get("name")
            if (
                isinstance(workspace_id, str)
                and isinstance(name, str)
                and _WORKSPACE_ID_RE.fullmatch(workspace_id)
                and workspace_id != DEFAULT_WORKSPACE_ID
            ):
                valid.append(
                    {
                        "id": workspace_id,
                        "name": name,
                        "created_at": str(entry.get("created_at", "")),
                    }
                )
        return valid

    def _write(self, entries: list[dict[str, str]]) -> None:
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {"version": INDEX_VERSION, "workspaces": entries}
        tmp = self._index_path.with_name("index.json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self._index_path)


__all__ = [
    "DEFAULT_WORKSPACE_ID",
    "DEFAULT_WORKSPACE_NAME",
    "NAME_MAX",
    "WorkspaceInfo",
    "WorkspaceRegistry",
    "validate_workspace_name",
]
