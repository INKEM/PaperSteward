from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from starlette.responses import Response

from papersteward.api.errors import install_error_handlers
from papersteward.errors import ConflictError, NotFoundError
from papersteward.observability.logging import request_id_context


async def _attach_request_id(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    request_id = request.headers.get("X-Request-ID", "test-request")
    request.state.request_id = request_id
    with request_id_context(request_id):
        return await call_next(request)


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = FastAPI()
    install_error_handlers(app)
    app.middleware("http")(_attach_request_id)

    @app.get("/papers/{paper_id}")
    async def get_paper(paper_id: str) -> dict[str, str]:
        raise NotFoundError(f"文献 {paper_id} 不存在", details={"paper_id": paper_id})

    @app.get("/conflict")
    async def conflict() -> dict[str, str]:
        raise ConflictError(details={"gate": "g4"})

    @app.get("/http-error")
    async def http_error() -> dict[str, str]:
        raise HTTPException(status_code=403, detail="没有权限")

    @app.get("/unauthorized")
    async def unauthorized() -> dict[str, str]:
        raise HTTPException(
            status_code=401,
            detail="未认证",
            headers={"WWW-Authenticate": "Bearer"},
        )

    @app.get("/structured-error")
    async def structured_error() -> dict[str, str]:
        raise HTTPException(
            status_code=400,
            detail={"message": "字段缺失", "field": "title"},
        )

    @app.get("/validation")
    async def validation(limit: int) -> dict[str, int]:
        return {"limit": limit}

    @app.get("/boom")
    async def boom() -> dict[str, str]:
        raise RuntimeError("secret-internal-detail")

    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def test_app_error_uses_common_envelope(client: TestClient) -> None:
    response = client.get("/papers/p001", headers={"X-Request-ID": "req-123"})

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "not_found",
            "message": "文献 p001 不存在",
            "details": {"paper_id": "p001"},
            "request_id": "req-123",
        }
    }


def test_unknown_route_uses_common_envelope(client: TestClient) -> None:
    response = client.get("/missing")

    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "not_found"
    assert error["message"] == "请求的资源不存在"
    assert error["request_id"] == "test-request"


def test_method_not_allowed_uses_common_envelope(client: TestClient) -> None:
    response = client.post("/validation")

    assert response.status_code == 405
    error = response.json()["error"]
    assert error["code"] == "method_not_allowed"
    assert error["message"] == "请求方法不被允许"


def test_http_exception_preserves_detail_and_headers(client: TestClient) -> None:
    response = client.get("/http-error")

    assert response.status_code == 403
    error = response.json()["error"]
    assert error["code"] == "http_error"
    assert error["message"] == "没有权限"

    unauthorized = client.get("/unauthorized")
    assert unauthorized.status_code == 401
    assert unauthorized.headers["www-authenticate"] == "Bearer"
    assert unauthorized.json()["error"]["message"] == "未认证"


def test_http_exception_accepts_structured_detail(client: TestClient) -> None:
    response = client.get("/structured-error")

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "http_error"
    assert error["message"] == "字段缺失"
    assert error["details"] == {"message": "字段缺失", "field": "title"}


def test_validation_error_has_field_details(client: TestClient) -> None:
    response = client.get("/validation?limit=not-a-number")

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["message"] == "请求参数校验失败"
    assert error["details"][0]["field"] == "query.limit"
    assert error["details"][0]["type"]
    assert error["request_id"] == "test-request"


def test_unhandled_exception_hides_details_but_logs(client: TestClient) -> None:
    response = client.get("/boom")

    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == "internal_error"
    assert error["message"] == "服务器内部错误，请查看日志"
    assert "secret-internal-detail" not in response.text
    assert error["request_id"] == "test-request"


def test_conflict_error_keeps_status_and_details(client: TestClient) -> None:
    response = client.get("/conflict")

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "conflict"
    assert error["details"] == {"gate": "g4"}
