import httpx
import pytest

from papersteward.sources.contracts import SourceError
from papersteward.sources.http import SourceHTTP

URL = "https://export.arxiv.org/api/query"


@pytest.mark.parametrize(
    "failure,code",
    [
        (429, "rate_limited"),
        (503, "unavailable"),
        (401, "unavailable"),
        (302, "unavailable"),
        ("timeout", "timeout"),
    ],
)
def test_failure_retries_and_sanitized_errors(failure, code):
    sleeps = []

    def handle(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("secret-value", request=request)
        return httpx.Response(failure)

    http = SourceHTTP("arxiv", transport=httpx.MockTransport(handle), sleep=sleeps.append)
    try:
        with pytest.raises(SourceError) as caught:
            http.get(URL, {"q": "secret-value"})
        assert caught.value.code == code and "secret-value" not in str(caught.value)
        assert http.request_count == (1 if failure in (401, 302) else 3)
    finally:
        http.close()


def test_retry_after_budget_cache_and_throttle():
    ticks, calls, sleeps = [0.0], [], []

    def sleep(seconds):
        sleeps.append(seconds)
        ticks[0] += seconds

    def handle(request):
        calls.append(request)
        return (
            httpx.Response(429, headers={"Retry-After": "5"})
            if len(calls) == 1
            else httpx.Response(200, content=b"ok")
        )

    http = SourceHTTP(
        "arxiv",
        transport=httpx.MockTransport(handle),
        clock=lambda: ticks[0],
        sleep=sleep,
        max_requests=2,
    )
    try:
        assert http.get(URL, {"q": 1}) == b"ok"
        assert 5 in sleeps and http.request_count == 2
        assert http.get(URL, {"q": 1}) == b"ok" and http.cache_hits == 1
        with pytest.raises(SourceError, match="budget_exceeded"):
            http.get(URL, {"q": 2})
        ticks[0] += 86401
        with pytest.raises(SourceError, match="budget_exceeded"):
            http.get(URL, {"q": 1})
    finally:
        http.close()


def test_long_retry_after_is_not_retried_early():
    http = SourceHTTP(
        "arxiv",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(429, headers={"Retry-After": "3600"})
        ),
    )
    try:
        with pytest.raises(SourceError, match="rate_limited"):
            http.get(URL, {})
        assert http.request_count == 1
        with pytest.raises(SourceError):
            http.get("https://evil.invalid/", {})
    finally:
        http.close()
