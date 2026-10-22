"""日报只追加读取；提交边界由调用方持有。"""

import hashlib
import json
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

from papersteward.core.errors import InvariantViolation
from papersteward.delivery.contracts import Digest
from papersteward.errors import NotFoundError
from papersteward.storage.models import DigestRow


def content_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def read_digest(session: Session, day: date) -> Digest:
    row = session.get(DigestRow, day)
    if row is None:
        raise NotFoundError("日报不存在；请先生成日报")
    if content_hash(row.payload_json) != row.content_hash:
        raise InvariantViolation("digest.hash", "日报内容校验失败")
    return Digest.model_validate(row.payload_json)


def append_digest(session: Session, digest: Digest, now: datetime) -> Digest:
    if session.get(DigestRow, digest.digest_date) is not None:
        raise InvariantViolation("digest.date", "日报只允许追加")
    payload = digest.model_dump(mode="json")
    session.add(
        DigestRow(
            digest_date=digest.digest_date,
            payload_json=payload,
            content_hash=content_hash(payload),
            created_at=now,
        )
    )
    session.flush()
    return digest
