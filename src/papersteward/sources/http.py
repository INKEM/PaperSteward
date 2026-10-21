"""受预算约束的同步 HTTP 边界；transport/时钟/等待均可离线注入。"""

import math
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

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
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if (
            not math.isfinite(timeout)
            or timeout <= 0
            or not math.isfinite(min_interval)
            or min_interval < 0
            or not 0 <= retries <= 5
            or max_requests < 1
        ):
            raise ValueError("HTTP 预算/超时/限流参数非法")
        self.source, self.retries, self.min_interval = source, retries, min_interval
        self.max_requests, self.clock, self.sleep = max_requests, clock, sleep
        self.request_count = self.cache_hits = 0
        self._last: float | None = None
        self._cache: dict[str, tuple[float, bytes]] = {}
        self._client = httpx.Client(
            transport=transport,
            timeout=timeout,
            trust_env=False,
            follow_redirects=False,
            headers={"User-Agent": "PaperSteward/0.1"},
        )

    def close(self) -> None:
        self._client.close()

    def get(self, url: str, params: Mapping[str, str | int]) -> bytes:
        target = httpx.URL(url)
        if (
            target.scheme != "https"
            or target.userinfo
            or target.host
            not in {"export.arxiv.org", "api.openalex.org", "api.semanticscholar.org"}
        ):
            raise SourceError(self.source, "invalid_response")
        key = str(target.copy_merge_params(sorted(params.items())))
        cached = self._cache.get(key)
        if cached and self.clock() - cached[0] < 86400:
            self.cache_hits += 1
            return cached[1]
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
                        if len(self._cache) >= 128:
                            self._cache.pop(next(iter(self._cache)))
                        self._cache[key] = (self.clock(), body)
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
