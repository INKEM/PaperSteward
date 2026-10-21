import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from test_api_dry_run import approval, body, feedback, setup_g3
from test_api_dry_run import client as client

from papersteward.core.entities import Config
from papersteward.core.enums import AssignedBy, EventType, RunStatus
from papersteward.core.errors import InvariantViolation
from papersteward.errors import ConflictError
from papersteward.pipeline.ingest import ingest
from papersteward.sources.contracts import QuerySpec, SourceError
from papersteward.sources.fixtures import FakeSourceAdapter
from papersteward.storage import create_session_factory, session_scope
from papersteward.storage.models import ConfigRow, EventRow, PaperRow, PaperSourceRow, RunRow
from papersteward.storage.repositories import ConfigRepository, EventRepository

NOW = datetime(2026, 10, 22, 0, tzinfo=UTC)
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/sources/basic.json"
QUERIES = [QuerySpec(keywords=("fixture",), max_results=10)]


def activate(client, **parameters):
    setup_g3(client)
    data = body()
    data["parameters"].update(parameters)
    response = client.post("/onboarding/dry-runs", json=data)
    assert response.status_code == 201, response.text
    view = response.json()
    view = client.post("/onboarding/dry-runs/1/feedback", json=feedback(view)).json()
    response = client.post("/onboarding/dry-runs/1/confirm", json=approval(view))
    assert response.status_code == 200, response.text


def run(session, run_id="run-one", **kwargs):
    return ingest(
        session,
        [FakeSourceAdapter(FIXTURE)],
        run_id=run_id,
        now=lambda: NOW,
        queries=QUERIES,
        **kwargs,
    )


def count(session, table):
    return session.scalar(select(func.count()).select_from(table))


def test_ingest_idempotency_provenance_and_dry_run(client, migrated_engine):
    activate(client)
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        baseline = count(session, PaperRow)
        preview = run(session, dry_run=True)
        assert preview.stats_json["dry_run"] and count(session, RunRow) == 0
        assert count(session, PaperRow) == baseline and count(session, PaperSourceRow) == 0
        first = run(session)
        assert first.status is RunStatus.SUCCEEDED and first.stats_json["new"] == 2
        assert first.stats_json["source_versions_added"] == 2
    with session_scope(factory) as session:
        second = run(session, run_id="run-two")
        assert second.stats_json["new"] == 0 and second.stats_json["existing"] == 2
        assert second.stats_json["source_versions_added"] == 0
        assert count(session, PaperRow) == baseline + 2 and count(session, PaperSourceRow) == 2
        assert {
            row.status for row in session.scalars(select(PaperRow).where(PaperRow.id.like("p_%")))
        } == {"screened"}
        with pytest.raises(InvariantViolation, match="run_id"):
            run(session, run_id="run-two")


def test_pre_g4_rejects_without_source_calls(migrated_engine):
    adapter = FakeSourceAdapter(FIXTURE)
    with session_scope(create_session_factory(migrated_engine)) as session:
        with pytest.raises(InvariantViolation, match="g4"):
            ingest(session, [adapter], run_id="closed", now=lambda: NOW, queries=QUERIES)
        assert not adapter.calls and count(session, RunRow) == 0


@pytest.mark.parametrize(
    "parameters",
    [{"paused": True}, {"quiet_start": "07:00", "quiet_end": "09:00", "push_time": "10:00"}],
)
def test_schedule_rejects_before_network(client, migrated_engine, parameters):
    activate(client, **parameters)
    with session_scope(create_session_factory(migrated_engine)) as session:
        with pytest.raises(InvariantViolation, match="schedule"):
            run(session)
        assert count(session, RunRow) == 0 and count(session, PaperSourceRow) == 0


def test_exact_config_reference_and_config_drift(client, migrated_engine):
    activate(client)
    with session_scope(create_session_factory(migrated_engine)) as session:
        ConfigRepository(session).add(
            Config(
                key="scoring",
                version="unrelated",
                value_json={},
                updated_by=AssignedBy.USER,
                updated_at=NOW + timedelta(days=1),
            )
        )
        assert run(session).stats_json["binding"]["scoring_version"] == "1.0.0"
        config = session.get(ConfigRow, ("onboarding_runtime", "1"))
        config.value_json = {**config.value_json, "paused": True}
        session.flush()
        with pytest.raises(ConflictError):
            run(session, run_id="drifted")


def test_source_failure_isolated_and_all_failed_reported(client, migrated_engine):
    activate(client)

    class Failed:
        source = "broken"

        def search(self, query):
            raise SourceError(self.source, "timeout", retryable=True)

    with session_scope(create_session_factory(migrated_engine)) as session:
        partial = ingest(
            session,
            [Failed(), FakeSourceAdapter(FIXTURE)],
            run_id="partial",
            now=lambda: NOW,
            queries=QUERIES,
        )
        assert partial.status is RunStatus.SUCCEEDED and partial.stats_json["partial"]
        assert (
            partial.stats_json["new"] == 2
            and partial.errors_json["sources"][0]["code"] == "timeout"
        )
        failed = ingest(session, [Failed()], run_id="failed", now=lambda: NOW, queries=QUERIES)
        assert failed.status is RunStatus.FAILED and not failed.stats_json["partial"]
        assert count(session, RunRow) == 2


def test_audit_failure_rolls_back_papers_but_keeps_failure_report(
    client, migrated_engine, monkeypatch
):
    activate(client)
    append = EventRepository.append

    def fail(self, event):
        if event.event_type is EventType.PAPER_DEDUPED:
            raise RuntimeError("synthetic audit failure")
        return append(self, event)

    monkeypatch.setattr(EventRepository, "append", fail)
    with session_scope(create_session_factory(migrated_engine)) as session:
        baseline = count(session, PaperRow), count(session, EventRow)
        report = run(session)
        assert report.status is RunStatus.FAILED and report.stats_json["new"] == 0
        assert (count(session, PaperRow), count(session, EventRow)) == baseline
        assert count(session, PaperSourceRow) == 0 and count(session, RunRow) == 1
        assert report.errors_json["sources"][-1]["code"] == "pipeline_failed"


def test_outer_transaction_rollback(client, migrated_engine):
    activate(client)
    factory = create_session_factory(migrated_engine)
    with pytest.raises(RuntimeError), session_scope(factory) as session:
        run(session)
        raise RuntimeError("outer abort")
    with session_scope(factory) as session:
        assert count(session, RunRow) == count(session, PaperSourceRow) == 0


@pytest.mark.parametrize("status", ["archived", "rejected", "scored"])
def test_existing_final_or_scored_metadata_is_unchanged(client, migrated_engine, tmp_path, status):
    activate(client)
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        run(session)
        row = session.scalar(select(PaperRow).where(PaperRow.doi == "10.0000/fixture-only"))
        identifier, title = row.id, row.title
        row.status = status  # 仅测试准备，验证采集不覆盖已进入后续流程的元数据。
    data = json.loads(FIXTURE.read_text())
    data["items"] = data["items"][:1]
    data["items"][0]["title"] = "Updated source title"
    data["items"][0]["provenance"]["raw_metadata"]["revision"] = 2
    path = tmp_path / "updated.json"
    path.write_text(json.dumps(data))
    with session_scope(factory) as session:
        report = ingest(
            session, [FakeSourceAdapter(path)], run_id="updated", now=lambda: NOW, queries=QUERIES
        )
        row = session.get(PaperRow, identifier)
        assert row.title == title and row.status == status
        assert report.stats_json["new"] == 0 and report.stats_json["source_versions_added"] == 1


def test_hard_filter_persists_rejected_reason(client, migrated_engine, tmp_path):
    activate(client)
    data = json.loads(FIXTURE.read_text())
    data["items"][0]["document_type"] = "news"
    path = tmp_path / "filtered.json"
    path.write_text(json.dumps(data))
    with session_scope(create_session_factory(migrated_engine)) as session:
        report = ingest(
            session, [FakeSourceAdapter(path)], run_id="filtered", now=lambda: NOW, queries=QUERIES
        )
        rejected = session.scalar(select(PaperRow).where(PaperRow.doi == "10.0000/fixture-only"))
        assert rejected.status == "rejected"
        assert report.stats_json["filtered"] == [
            {"paper_id": rejected.id, "reasons": ["document_type"]}
        ]
        event = session.scalar(
            select(EventRow).where(
                EventRow.paper_id == rejected.id,
                EventRow.event_type == EventType.PAPER_REJECTED.value,
            )
        )
        assert event.payload_json["reasons"] == ["document_type"]


def test_new_source_version_preserves_id_and_earliest_date(client, migrated_engine, tmp_path):
    activate(client)
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        run(session)
        row = session.scalar(select(PaperRow).where(PaperRow.doi == "10.0000/fixture-only"))
        identifier = row.id
    data = json.loads(FIXTURE.read_text())
    data["items"] = data["items"][:1]
    data["items"][0]["published_at"] = "2026-09-01T00:00:00Z"
    data["items"][0]["provenance"]["raw_metadata"]["revision"] = 2
    path = tmp_path / "earlier.json"
    path.write_text(json.dumps(data))
    with session_scope(factory) as session:
        report = ingest(
            session, [FakeSourceAdapter(path)], run_id="earlier", now=lambda: NOW, queries=QUERIES
        )
        row = session.get(PaperRow, identifier)
        assert report.stats_json["new"] == 0 and row.published_at.month == 9
        assert count(session, PaperSourceRow) == 3
        assert (
            session.scalar(
                select(func.count())
                .select_from(EventRow)
                .where(
                    EventRow.paper_id == identifier,
                    EventRow.event_type == EventType.PAPER_METADATA_UPDATED.value,
                )
            )
            == 1
        )
