from __future__ import annotations

from papersteward.errors import (
    AppError,
    ConfigurationError,
    ConflictError,
    ErrorCode,
    InvalidRequestError,
    NotFoundError,
)


def test_app_error_subclasses_have_stable_contract() -> None:
    assert AppError.code is ErrorCode.INTERNAL_ERROR
    assert AppError.status_code == 500

    assert InvalidRequestError.status_code == 400
    assert InvalidRequestError.code is ErrorCode.INVALID_REQUEST

    assert NotFoundError.status_code == 404
    assert NotFoundError.code is ErrorCode.NOT_FOUND

    assert ConflictError.status_code == 409
    assert ConflictError.code is ErrorCode.CONFLICT

    assert ConfigurationError.status_code == 500
    assert ConfigurationError.code is ErrorCode.CONFIGURATION_ERROR


def test_message_and_details_can_be_overridden() -> None:
    error = NotFoundError("文献 p001 不存在", details={"paper_id": "p001"})

    assert str(error) == "文献 p001 不存在"
    assert error.details == {"paper_id": "p001"}


def test_to_response_serializes_enum_and_request_id() -> None:
    response = ConflictError(details={"gate": "g4"}).to_response("req-1")
    body = response.model_dump(mode="json")

    assert body == {
        "error": {
            "code": "conflict",
            "message": "操作与当前状态冲突",
            "details": {"gate": "g4"},
            "request_id": "req-1",
        }
    }
