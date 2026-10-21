from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from io import StringIO

import pytest
from pydantic import SecretStr

from papersteward.config import Settings
from papersteward.observability.logging import (
    configure_logging,
    get_request_id,
    request_id_context,
    reset_request_id,
    set_request_id,
)


@pytest.fixture(autouse=True)
def restore_root_logger() -> Iterator[None]:
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    yield
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(level)


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_json_log_contains_standard_fields_and_context() -> None:
    stream = StringIO()
    configure_logging(_settings(log_level="DEBUG"), stream=stream)
    logger = logging.getLogger("papersteward.test")

    with request_id_context("req-123"):
        logger.info("处理完成", extra={"paper_id": "p001", "duration_ms": 12})

    payload = json.loads(stream.getvalue().strip())
    assert payload["level"] == "INFO"
    assert payload["logger"] == "papersteward.test"
    assert payload["message"] == "处理完成"
    assert payload["request_id"] == "req-123"
    assert payload["paper_id"] == "p001"
    assert payload["duration_ms"] == 12
    assert payload["timestamp"].endswith("Z")


def test_json_log_omits_request_id_when_absent() -> None:
    stream = StringIO()
    configure_logging(_settings(), stream=stream)

    logging.getLogger("papersteward.test").info("hello")

    payload = json.loads(stream.getvalue().strip())
    assert "request_id" not in payload


def test_sensitive_fields_are_redacted_recursively() -> None:
    stream = StringIO()
    configure_logging(_settings(), stream=stream)
    logger = logging.getLogger("papersteward.test")

    logger.info(
        "调用模型",
        extra={
            "api_key": "sk-secret-value",
            "payload": {"token": "plain-token", "model": "fake"},
            "tags": ["safe", {"password": "nested-secret"}],
            "credential": SecretStr("hidden-value"),
        },
    )

    raw = stream.getvalue()
    payload = json.loads(raw)
    assert "sk-secret-value" not in raw
    assert "plain-token" not in raw
    assert "nested-secret" not in raw
    assert "hidden-value" not in raw
    assert payload["api_key"] == "***"
    assert payload["payload"] == {"token": "***", "model": "fake"}
    assert payload["tags"] == ["safe", {"password": "***"}]
    assert payload["credential"] == "***"


def test_console_format_is_readable() -> None:
    stream = StringIO()
    configure_logging(_settings(log_format="console"), stream=stream)
    logger = logging.getLogger("papersteward.test")

    with request_id_context("req-9"):
        logger.info("本地开发", extra={"paper_id": "p001"})

    line = stream.getvalue().strip()
    assert "INFO" in line
    assert "本地开发" in line
    assert "request_id=req-9" in line
    assert "paper_id='p001'" in line


def test_level_filtering() -> None:
    stream = StringIO()
    configure_logging(_settings(log_level="WARNING"), stream=stream)
    logger = logging.getLogger("papersteward.test")

    logger.debug("hidden")
    logger.warning("shown")

    output = stream.getvalue()
    assert "hidden" not in output
    assert "shown" in output


def test_configure_logging_is_idempotent() -> None:
    stream = StringIO()
    settings = _settings()
    configure_logging(settings, stream=stream)
    configure_logging(settings, stream=stream)

    logging.getLogger("papersteward.test").info("once")

    lines = [line for line in stream.getvalue().splitlines() if line]
    assert len(lines) == 1


def test_exception_stack_is_logged() -> None:
    stream = StringIO()
    configure_logging(_settings(), stream=stream)

    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("papersteward.test").exception("失败")

    payload = json.loads(stream.getvalue().strip())
    assert "ValueError: boom" in payload["exception"]


def test_request_id_helpers_restore_previous_value() -> None:
    token = set_request_id("req-x")
    assert get_request_id() == "req-x"
    reset_request_id(token)
    assert get_request_id() is None

    with request_id_context("outer"):
        assert get_request_id() == "outer"
        with request_id_context("inner"):
            assert get_request_id() == "inner"
        assert get_request_id() == "outer"
    assert get_request_id() is None
