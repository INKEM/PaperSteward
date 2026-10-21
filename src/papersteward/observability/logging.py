"""结构化日志与请求上下文。

日志以 JSON Lines 输出，便于采集、grep 与回放；本地开发可切换为 console。
敏感字段（token、secret、password、api_key 等）在输出前统一脱敏。
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from typing import Any, TextIO

from pydantic import SecretStr

from papersteward.config import LogFormat, Settings, get_settings

_SENSITIVE_KEY = re.compile(
    r"(api[_-]?key|token|secret|password|passwd|authorization|cookie)",
    re.IGNORECASE,
)

# LogRecord 自带属性，不能当作业务 extra 输出。
_RESERVED_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)

_LEVELS: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

_request_id: ContextVar[str | None] = ContextVar("papersteward_request_id", default=None)


def get_request_id() -> str | None:
    """返回当前请求/任务关联的 request_id。"""
    return _request_id.get()


def set_request_id(value: str | None) -> Token[str | None]:
    """设置 request_id，返回用于恢复的 token；异步中间件使用。"""
    return _request_id.set(value)


def reset_request_id(token: Token[str | None]) -> None:
    """恢复到设置 request_id 之前的状态。"""
    _request_id.reset(token)


@contextmanager
def request_id_context(value: str | None) -> Iterator[None]:
    """在同步/异步代码块内绑定 request_id，退出时自动恢复。"""
    token = _request_id.set(value)
    try:
        yield
    finally:
        _request_id.reset(token)


def _timestamp(created: float) -> str:
    moment = datetime.fromtimestamp(created, tz=UTC)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _is_sensitive_key(key: str) -> bool:
    return _SENSITIVE_KEY.search(key) is not None


def _redact(value: Any) -> Any:
    if isinstance(value, SecretStr):
        return "***"
    if isinstance(value, Mapping):
        return {
            str(key): "***" if _is_sensitive_key(str(key)) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_redact(item) for item in value]
    return value


def _extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for key, value in record.__dict__.items():
        if key in _RESERVED_ATTRS or key.startswith("_") or key == "request_id":
            continue
        fields[key] = "***" if _is_sensitive_key(key) else _redact(value)
    return fields


class JsonLogFormatter(logging.Formatter):
    """输出单行 JSON；异常以 ``exception`` 字段保留堆栈。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": _timestamp(record.created),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = get_request_id()
        if request_id:
            payload["request_id"] = request_id
        payload.update(_extra_fields(record))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class ConsoleLogFormatter(logging.Formatter):
    """面向本地阅读的单行文本格式。"""

    def format(self, record: logging.LogRecord) -> str:
        parts = [
            _timestamp(record.created),
            record.levelname,
            record.name,
            record.getMessage(),
        ]
        request_id = get_request_id()
        if request_id:
            parts.append(f"request_id={request_id}")
        for key, value in _extra_fields(record).items():
            parts.append(f"{key}={value!r}")
        line = " ".join(parts)
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        if record.stack_info:
            line = f"{line}\n{self.formatStack(record.stack_info)}"
        return line


class _PaperStewardStreamHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """应用自己的 handler 标记，便于重复配置时精确替换。"""


def configure_logging(
    settings: Settings | None = None,
    *,
    stream: TextIO | None = None,
) -> None:
    """配置根日志器。

    可重复调用：旧的 PaperSteward handler 会被替换，不会重复输出；
    其它库（pytest、uvicorn）的 handler 保持不变。
    """
    settings = settings or get_settings()
    formatter: logging.Formatter = (
        ConsoleLogFormatter() if settings.log_format is LogFormat.CONSOLE else JsonLogFormatter()
    )

    root = logging.getLogger()
    for existing in list(root.handlers):
        if isinstance(existing, _PaperStewardStreamHandler):
            root.removeHandler(existing)
            existing.close()

    handler = _PaperStewardStreamHandler(stream or sys.stderr)
    handler.setFormatter(formatter)
    root.addHandler(handler)
    root.setLevel(_LEVELS[settings.log_level])


__all__ = [
    "ConsoleLogFormatter",
    "JsonLogFormatter",
    "configure_logging",
    "get_request_id",
    "request_id_context",
    "reset_request_id",
    "set_request_id",
]
