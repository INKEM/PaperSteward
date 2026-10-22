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
    # 宿主机 .env 可能已配置真实 LLM；测试默认固定为离线 fake，防止意外联网/计费。
    monkeypatch.setenv("PAPERSTEWARD_LLM_PROVIDER", "fake")
    monkeypatch.setenv("PAPERSTEWARD_LLM_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("PAPERSTEWARD_LLM_MODEL", "")
    monkeypatch.setenv("PAPERSTEWARD_LLM_API_KEY", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
