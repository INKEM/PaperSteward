from datetime import timedelta

import pytest
from sqlalchemy import func, select
from test_api_dry_run import client as client
from test_pipeline_ingest import activate
from test_priority_queue import DAY, NOW, seed

from papersteward.cli.main import main
from papersteward.config import Settings, get_settings
from papersteward.core.enums import PaperStatus
from papersteward.core.errors import InvariantViolation
from papersteward.delivery.service import create_digest, deliver
from papersteward.storage import create_session_factory, session_scope
from papersteward.storage.models import DeliveryRow, DigestRow
from papersteward.storage.repositories import PaperRepository


def prepared(client, engine, **parameters):
    activate(client, **parameters)
    factory = create_session_factory(engine)
    with session_scope(factory) as session:
        seed(session)
        create_digest(session, DAY, now=NOW)
    return factory


def test_gate_api_generation_and_daily_idempotency(client, migrated_engine, monkeypatch):
    monkeypatch.setattr("papersteward.api.delivery.utc_now", lambda: NOW)
    assert client.post("/digests", json={"digest_date": str(DAY)}).status_code == 409
    factory = prepared(client, migrated_engine)
    first = client.get(f"/digests/{DAY}")
    assert first.status_code == 200 and first.json()["items"][0]["paper_id"] == "p"
    assert client.post("/digests", json={"digest_date": str(DAY)}).json() == first.json()
    assert "<!doctype html>" in client.get(f"/digests/{DAY}?format=html").text
    assert "# 今日文献" in client.get(f"/digests/{DAY}?format=markdown").text
    with session_scope(factory) as session:
        assert session.scalar(select(func.count()).select_from(DigestRow)) == 1


def test_file_delivery_no_overwrite_and_cooldown(client, migrated_engine, tmp_path):
    factory = prepared(client, migrated_engine)
    path = tmp_path / "out.md"
    settings = Settings(_env_file=None)
    assert not deliver(factory, DAY, now=NOW, settings=settings, out=path)["existing"]
    assert path.stat().st_mode & 0o777 == 0o600
    content = path.read_text()
    assert deliver(factory, DAY, now=NOW, settings=settings, out=path)["existing"]
    assert path.read_text() == content
    with pytest.raises(InvariantViolation, match="目标"):
        deliver(factory, DAY, now=NOW, settings=settings, out=tmp_path / "other.md")
    with session_scope(factory) as session:
        assert PaperRepository(session).get("p").status is PaperStatus.PUSHED
        next_day = create_digest(session, DAY + timedelta(days=1), now=NOW + timedelta(days=1))
        assert next_day.items == [] and next_day.deferred["p"] == "already_pushed"


def test_file_failure_and_explicit_recovery(client, migrated_engine, tmp_path):
    factory = prepared(client, migrated_engine)
    path = tmp_path / "out.md"
    path.write_text("keep")
    with pytest.raises(InvariantViolation, match="文件输出失败"):
        deliver(factory, DAY, now=NOW, settings=Settings(_env_file=None), out=path)
    assert path.read_text() == "keep"
    with pytest.raises(InvariantViolation, match="自动重发"):
        deliver(factory, DAY, now=NOW, settings=Settings(_env_file=None), out=path)
    path.unlink()
    assert (
        deliver(
            factory, DAY, now=NOW, settings=Settings(_env_file=None), out=path, recover_file=True
        )["status"]
        == "sent"
    )


def test_smtp_failure_is_durable_and_file_still_works(
    client, migrated_engine, tmp_path, monkeypatch
):
    factory = prepared(client, migrated_engine, channel="smtp")
    calls = []

    def broken(*args):
        calls.append(1)
        raise OSError("sensitive-provider-error")

    monkeypatch.setattr("papersteward.delivery.service.send_smtp", broken)
    settings = Settings(
        _env_file=None, smtp_host="example.org", smtp_from="a@example.org", smtp_to="b@example.org"
    )
    with pytest.raises(InvariantViolation, match="送达结果未知") as error:
        deliver(factory, DAY, now=NOW, settings=settings, channel="smtp")
    assert "sensitive-provider-error" not in str(error.value)
    with pytest.raises(InvariantViolation, match="自动重发"):
        deliver(factory, DAY, now=NOW, settings=settings, channel="smtp")
    assert len(calls) == 1
    assert (
        deliver(factory, DAY, now=NOW, settings=settings, out=tmp_path / "fallback.md")["status"]
        == "sent"
    )
    with session_scope(factory) as session:
        assert session.get(DeliveryRow, (DAY, "smtp")).status == "uncertain"


def test_smtp_requires_g4_and_generation_outer_rollback(client, migrated_engine):
    factory = prepared(client, migrated_engine)
    settings = Settings(
        _env_file=None, smtp_host="example.org", smtp_from="a@example.org", smtp_to="b@example.org"
    )
    with pytest.raises(InvariantViolation, match="G4未确认"):
        deliver(factory, DAY, now=NOW, settings=settings, channel="smtp")
    with factory() as session:
        create_digest(session, DAY + timedelta(days=1), now=NOW + timedelta(days=1))
        session.rollback()
    with session_scope(factory) as session:
        assert session.get(DigestRow, DAY + timedelta(days=1)) is None
        assert session.get(DeliveryRow, (DAY, "smtp")) is None


def test_digest_cli(client, migrated_engine, tmp_path, monkeypatch, capsys):
    prepared(client, migrated_engine)
    monkeypatch.setenv("PAPERSTEWARD_DATABASE_URL", str(migrated_engine.url))
    monkeypatch.setattr("papersteward.cli.delivery.utc_now", lambda: NOW)
    get_settings.cache_clear()
    try:
        args = ["digest", "--date", str(DAY), "--out", str(tmp_path / "cli.md"), "--json"]
        assert main(args) == 0 and '"status": "sent"' in capsys.readouterr().out
        assert main(args) == 0 and '"existing": true' in capsys.readouterr().out
    finally:
        get_settings.cache_clear()


def test_smtp_reservation_committed_before_io_and_repeat_does_not_send(
    client, migrated_engine, monkeypatch
):
    factory = prepared(client, migrated_engine, channel="smtp")
    calls = []

    def send(*args):
        with session_scope(factory) as session:
            assert session.get(DeliveryRow, (DAY, "smtp")).status == "reserved"
        calls.append(1)

    monkeypatch.setattr("papersteward.delivery.service.send_smtp", send)
    settings = Settings(
        _env_file=None, smtp_host="example.org", smtp_from="a@example.org", smtp_to="b@example.org"
    )
    assert not deliver(factory, DAY, now=NOW, settings=settings, channel="smtp")["existing"]
    assert deliver(factory, DAY, now=NOW, settings=settings, channel="smtp")["existing"]
    assert calls == [1]


def test_digest_audit_failure_rolls_back_when_caller_catches(client, migrated_engine, monkeypatch):
    activate(client)
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        seed(session)
        with monkeypatch.context() as m:

            def broken(*args, **kwargs):
                raise RuntimeError("audit failed")

            m.setattr("papersteward.delivery.service.audit", broken)
            with pytest.raises(RuntimeError):
                create_digest(session, DAY, now=NOW)
        assert session.get(DigestRow, DAY) is None
        assert PaperRepository(session).get("p").status is PaperStatus.CLASSIFIED


def test_uncertain_smtp_does_not_reappear_in_next_daily_digest(
    client, migrated_engine, monkeypatch
):
    factory = prepared(client, migrated_engine, channel="smtp")

    def broken(*args):
        raise OSError("unknown receipt")

    monkeypatch.setattr("papersteward.delivery.service.send_smtp", broken)
    settings = Settings(
        _env_file=None, smtp_host="example.org", smtp_from="a@example.org", smtp_to="b@example.org"
    )
    with pytest.raises(InvariantViolation, match="送达结果未知"):
        deliver(factory, DAY, now=NOW, settings=settings, channel="smtp")
    with session_scope(factory) as session:
        digest = create_digest(session, DAY + timedelta(days=1), now=NOW + timedelta(days=1))
        assert digest.items == [] and digest.deferred["p"] == "already_pushed"


@pytest.mark.parametrize(
    "parameters",
    [{"paused": True}, {"quiet_start": "15:00", "quiet_end": "17:00", "push_time": "08:00"}],
)
def test_digest_schedule_rejects_without_writes(client, migrated_engine, parameters):
    activate(client, **parameters)
    with session_scope(create_session_factory(migrated_engine)) as session:
        with pytest.raises(InvariantViolation, match="暂停或安静"):
            create_digest(session, DAY, now=NOW)
        assert session.get(DigestRow, DAY) is None
