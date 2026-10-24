"""来源适配器组装：fixture 回放或显式 live；供 CLI 与挖掘任务共用。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import httpx

from papersteward.config import Settings
from papersteward.sources.arxiv import ArxivAdapter
from papersteward.sources.contracts import SourceAdapter
from papersteward.sources.fixtures import FakeSourceAdapter
from papersteward.sources.http import SourceHTTP
from papersteward.sources.openalex import OpenAlexAdapter
from papersteward.sources.semantic_scholar import SemanticScholarAdapter

SOURCES = ("arxiv", "openalex", "semantic_scholar", "fake")


def fixture_transport(path: Path) -> httpx.MockTransport:
    """原始响应文件重复回放；`{"responses": [...]}` 按请求顺序回放多页响应。"""
    body = path.read_bytes()
    try:
        cassette = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        cassette = None
    if not (isinstance(cassette, dict) and isinstance(cassette.get("responses"), list)):
        return httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    responses: list[bytes] = []
    for item in cassette["responses"]:
        if isinstance(item, str):
            responses.append(item.encode("utf-8"))
        elif isinstance(item, (dict, list)):
            responses.append(json.dumps(item, ensure_ascii=False).encode("utf-8"))
        else:
            raise ValueError("fixture responses 只支持对象、数组或字符串")
    if not responses:
        raise ValueError("fixture responses 不能为空")
    pending = iter(responses)

    def handle(request: httpx.Request) -> httpx.Response:
        try:
            content = next(pending)
        except StopIteration:
            # 不重复最后一页，避免把分页耗尽伪装成可解析的重复候选。
            return httpx.Response(400, content=b'{"error":"fixture responses exhausted"}')
        return httpx.Response(200, content=content)

    return httpx.MockTransport(handle)


def build_adapters(
    sources: Sequence[str],
    settings: Settings,
    clients: list[SourceHTTP],
    *,
    fixtures: Mapping[str, Path] | None = None,
    live: bool = False,
) -> list[SourceAdapter]:
    """按来源列表组装适配器；fixture 回放优先，真实 HTTP 必须显式 live。

    创建的 ``SourceHTTP`` 客户端追加到 ``clients``，由调用方负责关闭。
    """
    fixtures = dict(fixtures or {})
    unknown = [source for source in sources if source not in SOURCES]
    if unknown:
        raise ValueError(f"未知来源：{unknown}")
    if len(set(sources)) != len(sources):
        raise ValueError("来源不能重复")
    duplicated = sorted(set(fixtures) - set(sources))
    if duplicated:
        raise ValueError(f"fixture来源未知：{duplicated}")
    result: list[SourceAdapter] = []
    for source in sources:
        path = fixtures.get(source)
        if source == "fake":
            if path is None:
                raise ValueError("fake来源必须提供fixture")
            result.append(FakeSourceAdapter(path))
            continue
        if path is None and not live:
            raise ValueError("请提供--fixture；真实HTTP调用须显式--live")
        transport = fixture_transport(path) if path else None
        client = SourceHTTP(
            source,
            transport=transport,
            min_interval=0 if path else 3,
            api_key=settings.semantic_scholar_api_key
            if source == "semantic_scholar" and not path
            else None,
        )
        clients.append(client)
        factory: type[ArxivAdapter] | type[OpenAlexAdapter] | type[SemanticScholarAdapter] = {
            "arxiv": ArxivAdapter,
            "openalex": OpenAlexAdapter,
            "semantic_scholar": SemanticScholarAdapter,
        }[source]
        result.append(factory(client))
    return result


__all__ = ["SOURCES", "build_adapters", "fixture_transport"]
