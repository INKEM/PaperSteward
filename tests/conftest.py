"""跨测试共享的 fixture。"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from papersteward.config import get_settings


@pytest.fixture(autouse=True)
def clean_papersteward_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """隔离宿主机环境，避免真实 .env / 环境变量影响测试结果。"""
    for key in list(os.environ):
        if key.startswith("PAPERSTEWARD_"):
            monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
