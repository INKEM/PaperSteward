"""本地 HTTP 与已审快照；不连接数据库，不自动重试或迁移。"""

import json
import os
from pathlib import Path
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from pydantic import BaseModel, ConfigDict, Field

from papersteward.core.onboarding import OnboardingStage
from papersteward.onboarding.calibration import report_hash

MAX_BYTES = 4 * 1024 * 1024
Kind = Literal["profile", "taxonomy", "calibration", "dry-run"]


class ClientError(Exception):
    pass


def local_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or (port is not None and not 1 <= port <= 65535)
        ):
            raise ValueError
    except ValueError as exc:
        raise ClientError("--url 需要本地 API 根地址，例如 http://127.0.0.1:8000") from exc
    host = "[::1]" if parsed.hostname == "::1" else parsed.hostname
    return f"{parsed.scheme}://{host}" + (f":{port}" if port is not None else "")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


class Client:
    def __init__(self, url: str) -> None:
        self.url = local_url(url)
        self.opener = build_opener(ProxyHandler({}), _NoRedirect())

    def request(self, method: str, path: str, body: Any = None) -> dict[str, Any]:
        data = (
            None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
        )
        request = Request(
            self.url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with self.opener.open(request, timeout=10) as response:
                raw = response.read(MAX_BYTES + 1)
        except HTTPError as exc:
            try:
                error = json.loads(exc.read(MAX_BYTES)).get("error", {})
                message = error.get("message", "请求被拒绝")
                details = error.get("details")
                field = (
                    "; ".join(f"{item['field']}: {item['message']}" for item in details[:3])
                    if isinstance(details, list)
                    else details.get("field", "")
                    if isinstance(details, dict)
                    else ""
                )
                request_id = error.get("request_id", "")
                detail = f"{message} {field} request_id={request_id}".strip()
            except (ValueError, AttributeError, TypeError):
                detail = "API 未返回标准错误信封"
            hint = (
                "请重新 review 并保存新文件，再明确确认"
                if exc.code == 409
                else "请检查输入或服务器日志"
            )
            raise ClientError(f"HTTP {exc.code}：{detail}；{hint}；变更不会自动重试") from exc
        except (URLError, OSError, TimeoutError) as exc:
            raise ClientError(
                "无法连接本地 API；请先迁移数据库并启动 uvicorn，检查 --url；变更不会自动重试"
            ) from exc
        try:
            result = json.loads(raw)
            if len(raw) > MAX_BYTES or not isinstance(result, dict):
                raise ValueError
        except (ValueError, UnicodeError) as exc:
            raise ClientError("API 响应不是有效的 JSON 对象或超过 4 MiB；请检查服务器") from exc
        return result


def load_json(path: str) -> Any:
    try:
        with Path(path).open("rb") as source:
            raw = source.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError
        return json.loads(raw)
    except (OSError, ValueError, UnicodeError) as exc:
        raise ClientError("无法读取 JSON 文件；请检查路径、UTF-8 格式和 4 MiB 大小限制") from exc


class ReviewSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1]
    server_url: str
    kind: Kind
    stage: OnboardingStage
    revision: int = Field(ge=0, strict=True)
    data: dict[str, Any]
    checksum: str = Field(pattern=r"^[0-9a-f]{64}$")


def snapshot(
    client: Client, kind: Kind, state: dict[str, Any], data: dict[str, Any]
) -> ReviewSnapshot:
    value = {
        "schema_version": 1,
        "server_url": client.url,
        "kind": kind,
        "stage": state["stage"],
        "revision": state["revision"],
        "data": data,
    }
    return ReviewSnapshot.model_validate({**value, "checksum": report_hash(value)})


def read_snapshot(path: str, client: Client) -> ReviewSnapshot:
    try:
        value = ReviewSnapshot.model_validate(load_json(path))
        if value.checksum != report_hash(value.model_dump(mode="json", exclude={"checksum"})):
            raise ValueError
    except ValueError as exc:
        raise ClientError("审阅快照不完整或已被编辑；请重新 review，使用新文件名保存") from exc
    if value.server_url != client.url:
        raise ClientError("审阅快照来自另一 API 地址；请在当前 --url 下重新 review")
    return value


def check_output(path: str | None) -> None:
    if path is not None and (Path(path).exists() or Path(path).is_symlink()):
        raise ClientError("--out 文件已存在；请使用新文件名，保留此前审阅依据")


def save_snapshot(path: str, value: ReviewSnapshot) -> None:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(value.model_dump_json(indent=2) + "\n")
    except OSError as exc:
        raise ClientError(
            "服务端操作已完成，但无法保存 --out；请用 review 和新文件名恢复，勿重复提交变更"
        ) from exc
