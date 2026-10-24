"""LLM 结果缓存：命中跳过 provider、损坏回源、DatabaseLLMCache 落库读写。"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from papersteward.llm import FakeLLM, LLMGateway
from papersteward.llm.cache import DatabaseLLMCache
from papersteward.llm.gateway import LLMCacheEntry
from papersteward.storage import Base, create_db_engine, create_session_factory

NOW = datetime(2026, 10, 23, 8, tzinfo=UTC)
RELEVANCE = {
    "problem": 3,
    "method": 3,
    "domain": 3,
    "actionability": 3,
    "evidence": [{"dimension": "problem", "quote": "q"}],
    "confidence": 0.8,
    "boundary_note": None,
}
VARIABLES = {"profile": "p", "title": "t", "abstract": "a"}


class MemoryCache:
    """测试用缓存协议实现；记录 put 次数。"""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str, str], LLMCacheEntry] = {}
        self.puts = 0

    def get(self, *, model, prompt_version, input_hash):
        return self.store.get((model, prompt_version, input_hash))

    def put(self, entry, *, model, prompt_version, input_hash):
        self.puts += 1
        self.store[(model, prompt_version, input_hash)] = entry


def _payload() -> str:
    return json.dumps(RELEVANCE, ensure_ascii=False)


def test_cache_hit_skips_provider_call() -> None:
    provider = FakeLLM([_payload()], model_id="fake-llm")
    cache = MemoryCache()
    gateway = LLMGateway(provider, cache=cache, clock=lambda: NOW)
    first = gateway.complete_json("relevance", VARIABLES)
    assert provider.call_count == 1 and cache.puts == 1
    second = gateway.complete_json("relevance", VARIABLES)
    assert provider.call_count == 1  # 命中缓存，不再调用 provider
    assert second.data == first.data
    assert second.attempts == 0  # 缓存命中记零次尝试
    assert second.usage.calls == 0 and second.usage.cost_usd == 0.0
    assert second.prompt_version == first.prompt_version


def test_cache_miss_on_different_input() -> None:
    provider = FakeLLM([_payload()], model_id="fake-llm")
    cache = MemoryCache()
    gateway = LLMGateway(provider, cache=cache, clock=lambda: NOW)
    gateway.complete_json("relevance", VARIABLES)
    gateway.complete_json("relevance", {**VARIABLES, "title": "different"})
    assert provider.call_count == 2


def test_corrupt_cache_entry_falls_back_to_provider() -> None:
    provider = FakeLLM([_payload()], model_id="fake-llm")
    cache = MemoryCache()
    gateway = LLMGateway(provider, cache=cache, clock=lambda: NOW)
    gateway.complete_json("relevance", VARIABLES)
    # 模拟缓存内容损坏：无法通过 schema 校验时按未命中重新调用。
    key = next(iter(cache.store))
    cache.store[key] = LLMCacheEntry(
        raw_text="not-json", repair_used=False, prompt_version=key[1], generated_at=NOW
    )
    gateway.complete_json("relevance", VARIABLES)
    assert provider.call_count == 2


def test_database_cache_round_trip_and_isolation() -> None:
    engine = create_db_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    cache = DatabaseLLMCache(factory)
    assert cache.get(model="m", prompt_version="v1", input_hash="h1") is None
    cache.put(
        LLMCacheEntry(raw_text=_payload(), repair_used=True, prompt_version="v1", generated_at=NOW),
        model="m",
        prompt_version="v1",
        input_hash="h1",
    )
    entry = cache.get(model="m", prompt_version="v1", input_hash="h1")
    assert entry is not None
    assert entry.repair_used is True and entry.prompt_version == "v1"
    # SQLite 往返会丢时区；缓存层必须按全库约定补回 UTC，否则缓存命中会把
    # naive 时间带进 Provenance 实体校验（“必须带时区”挖掘失败）。
    assert entry.generated_at.tzinfo is not None
    assert entry.generated_at == NOW
    # 重复 put 幂等；不同键互不可见。
    cache.put(
        LLMCacheEntry(
            raw_text=_payload(), repair_used=False, prompt_version="v1", generated_at=NOW
        ),
        model="m",
        prompt_version="v1",
        input_hash="h1",
    )
    assert cache.get(model="m", prompt_version="v1", input_hash="h2") is None
    assert cache.get(model="other", prompt_version="v1", input_hash="h1") is None
    again = cache.get(model="m", prompt_version="v1", input_hash="h1")
    assert again is not None and again.repair_used is True  # 首次写入未被覆盖
    engine.dispose()


def test_database_cache_hit_keeps_provenance_valid() -> None:
    """真实 SQLite 往返后的缓存命中：溯源时间戳仍可构造 Provenance 实体。"""
    from papersteward.core.entities import Provenance

    engine = create_db_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    provider = FakeLLM([_payload()], model_id="fake-llm")
    gateway = LLMGateway(provider, cache=DatabaseLLMCache(factory), clock=lambda: NOW)
    first = gateway.complete_json("relevance", VARIABLES)
    assert provider.call_count == 1
    second = gateway.complete_json("relevance", VARIABLES)  # 命中数据库缓存
    assert provider.call_count == 1 and second.attempts == 0
    provenance = Provenance(
        model=second.provenance.model,
        prompt_version=second.provenance.prompt_version,
        generated_at=second.provenance.generated_at,
        input_hash=second.provenance.input_hash,
    )
    assert provenance.generated_at.tzinfo is not None
    engine.dispose()


def test_database_cache_swallows_internal_errors() -> None:
    """缓存自身的故障绝不阻塞主流程：get 按未命中、put 静默丢弃。"""
    engine = create_db_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    cache = DatabaseLLMCache(factory)
    engine.dispose()  # 连接池销毁后所有查询都会失败
    assert cache.get(model="m", prompt_version="v1", input_hash="h") is None
    cache.put(
        LLMCacheEntry(raw_text="x", repair_used=False, prompt_version="v1", generated_at=NOW),
        model="m",
        prompt_version="v1",
        input_hash="h",
    )  # 不抛异常即通过
