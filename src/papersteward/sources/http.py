"""受预算约束的同步 HTTP 边界；transport/时钟/等待均可离线注入。"""

import math
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx
from pydantic import SecretStr

from papersteward.sources.contracts import SourceError


class SourceHTTP:
    def __init__(
        self,
        source: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 20,
        retries: int = 2,
        min_interval: float = 3,
        max_requests: int = 30,
        cache_max_entries: int = 128,
        cache_max_bytes: int = 32 * 1024 * 1024,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        api_key: SecretStr | None = None,
    ) -> None:
        if (
            not math.isfinite(timeout)
            or timeout <= 0
            or not math.isfinite(min_interval)
            or min_interval < 0
            or not 0 <= retries <= 5
            or max_requests < 1
            or cache_max_entries < 1
            or cache_max_bytes < 1
        ):
            raise ValueError("HTTP 预算/超时/限流/缓存参数非法")
        self.source, self.retries, self.min_interval = source, retries, min_interval
        self.max_requests, self.clock, self.sleep = max_requests, clock, sleep
        self.cache_max_entries, self.cache_max_bytes = cache_max_entries, cache_max_bytes
        self.request_count = self.cache_hits = self.cache_bytes = 0
        self._last: float | None = None
        self._cache: dict[str, tuple[float, bytes]] = {}
        headers = {"User-Agent": "PaperSteward/0.1"}
        if api_key is not None:
            if source != "semantic_scholar":
                raise ValueError("此来源不支持密钥头")
            headers["x-api-key"] = api_key.get_secret_value()
        self._client = httpx.Client(
            transport=transport,
            timeout=timeout,
            trust_env=False,
            follow_redirects=False,
            headers=headers,
        )

    def close(self) -> None:
        self._client.close()

    def _remember(self, key: str, body: bytes) -> None:
        """按条数和总字节数双重限制的 FIFO 缓存；超大响应只返回不缓存。"""
        if len(body) > self.cache_max_bytes:
            return
        while self._cache and (
            len(self._cache) >= self.cache_max_entries
            or self.cache_bytes + len(body) > self.cache_max_bytes
        ):
            oldest = next(iter(self._cache))
            _, evicted = self._cache.pop(oldest)
            self.cache_bytes -= len(evicted)
        self._cache[key] = (self.clock(), body)
        self.cache_bytes += len(body)

    def get(self, url: str, params: Mapping[str, str | int]) -> bytes:
        target = httpx.URL(url)
        expected_host = {
            "arxiv": "export.arxiv.org",
            "openalex": "api.openalex.org",
            "semantic_scholar": "api.semanticscholar.org",
        }.get(self.source)
        if target.scheme != "https" or target.userinfo or target.host != expected_host:
            raise SourceError(self.source, "invalid_response")
        key = str(target.copy_merge_params(sorted(params.items())))
        cached = self._cache.get(key)
        if cached is not None:
            if self.clock() - cached[0] < 86400:
                self.cache_hits += 1
                return cached[1]
            self._cache.pop(key)
            self.cache_bytes -= len(cached[1])
        for attempt in range(self.retries + 1):
            if self.request_count >= self.max_requests:
                raise SourceError(self.source, "budget_exceeded")
            if self._last is not None:
                self.sleep(max(0, self.min_interval - (self.clock() - self._last)))
            self._last = self.clock()
            self.request_count += 1
            delay = min(2.0**attempt, 8)
            try:
                with self._client.stream("GET", url, params=params) as response:
                    if response.status_code == 200:
                        chunks: list[bytes] = []
                        size = 0
                        for chunk in response.iter_bytes():
                            size += len(chunk)
                            if size > 8 * 1024 * 1024:
                                raise SourceError(self.source, "invalid_response")
                            chunks.append(chunk)
                        body = b"".join(chunks)
                        self._remember(key, body)
                        return body
                    retryable = response.status_code == 429 or response.status_code >= 500
                    error = SourceError(
                        self.source,
                        "rate_limited" if response.status_code == 429 else "unavailable",
                        retryable=retryable,
                    )
                    retry_after = response.headers.get("Retry-After")
                    if retry_after:
                        try:
                            wait = float(retry_after)
                        except ValueError:
                            try:
                                wait = (
                                    parsedate_to_datetime(retry_after) - datetime.now(UTC)
                                ).total_seconds()
                            except (ValueError, TypeError, OverflowError):
                                wait = delay
                        if not math.isfinite(wait) or wait > 60:
                            raise error
                        delay = max(delay, wait)
            except httpx.TimeoutException:
                error = SourceError(self.source, "timeout", retryable=True)
            except httpx.RequestError:
                error = SourceError(self.source, "unavailable", retryable=True)
            if not error.retryable or attempt == self.retries:
                raise error
            self.sleep(delay)
        raise AssertionError("unreachable")
