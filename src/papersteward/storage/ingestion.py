"""采集仓储扩展；只flush，不提交。"""

import hashlib
import json
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from papersteward.core.entities import Run
from papersteward.core.enums import RunStatus
from papersteward.core.errors import InvariantViolation
from papersteward.sources.contracts import PaperCandidate
from papersteward.storage.mappers import run_from_row
from papersteward.storage.models import PaperSourceRow, RunRow
from papersteward.storage.repositories import RunRepository


def attach_source(session: Session, paper_id: str, candidate: PaperCandidate) -> bool:
    proof = candidate.provenance
    identity = json.dumps(
        [proof.source, proof.source_id, proof.raw_metadata],
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    identifier = hashlib.sha256(identity.encode()).hexdigest()
    previous = session.get(PaperSourceRow, identifier)
    if previous is not None:
        if previous.paper_id != paper_id:
            raise InvariantViolation("paper_source.identity", "来源记录不能静默重新归属")
        return False
    session.add(
        PaperSourceRow(
            id=identifier,
            paper_id=paper_id,
            source=proof.source,
            source_id=proof.source_id,
            url=proof.url,
            retrieved_at=proof.retrieved_at,
            raw_json=dict(proof.raw_metadata),
            candidate_json=candidate.model_dump(mode="json"),
        )
    )
    session.flush()
    return True


def finish_run(
    session: Session,
    run_id: str,
    status: RunStatus,
    finished_at: datetime,
    stats: dict[str, Any],
    errors: dict[str, Any],
) -> Run:
    RunRepository(session).transition(run_id, status, finished_at=finished_at)
    row = session.get(RunRow, run_id)
    if row is None:
        raise InvariantViolation("run.id", "运行记录不存在")
    row.stats_json, row.errors_json = stats, errors
    session.flush()
    return run_from_row(row)
