from __future__ import annotations

import socket
import threading

from sqlalchemy import inspect

from papersteward.cli.main import main
from papersteward.cli.start import _open_when_ready
from papersteward.storage import create_db_engine
from papersteward.storage.migrations import get_migration_status


def test_start_explicit_workspace_migrates_and_uses_same_engine(tmp_path, monkeypatch):
    unrelated = tmp_path / "unrelated.db"
    monkeypatch.setenv("PAPERSTEWARD_DATABASE_URL", f"sqlite+pysqlite:///{unrelated}")
    workspace = tmp_path / "workspace"
    calls = []

    def serve(server, *, sockets):
        assert sockets[0].getsockname()[1] > 0
        calls.append(server.config.app.state.settings.db_url)

    monkeypatch.setattr("uvicorn.Server.run", serve)
    monkeypatch.setattr("webbrowser.open", lambda *_: (_ for _ in ()).throw(AssertionError()))
    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
    assert main(["start", "--data-dir", str(workspace), "--port", str(port), "--no-browser"]) == 0
    database = workspace / "papersteward.db"
    assert calls == [f"sqlite+pysqlite:///{database}"]
    assert not unrelated.exists()
    engine = create_db_engine(calls[0])
    try:
        assert get_migration_status(engine).current == "0018"
        assert "paper" in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_start_port_conflict_leaves_database_untouched(tmp_path, monkeypatch, capsys):
    with socket.create_server(("127.0.0.1", 0)) as occupied:
        port = occupied.getsockname()[1]
        assert (
            main(["start", "--data-dir", str(tmp_path), "--port", str(port), "--no-browser"]) == 1
        )
    assert "端口" in capsys.readouterr().err
    assert not (tmp_path / "papersteward.db").exists()


def test_start_directory_and_migration_failures_are_actionable(tmp_path, monkeypatch, capsys):
    path = tmp_path / "file"
    path.write_text("occupied")
    assert main(["start", "--data-dir", str(path), "--no-browser"]) == 1
    assert "数据目录不可写" in capsys.readouterr().err
    monkeypatch.setattr(
        "papersteward.cli.start.migrate", lambda *_: (_ for _ in ()).throw(FileNotFoundError())
    )
    assert main(["start", "--data-dir", str(tmp_path), "--port", "39875", "--no-browser"]) == 1
    assert "迁移失败" in capsys.readouterr().err


def test_browser_waits_for_successful_health(monkeypatch):
    import httpx

    opened = []
    monkeypatch.setattr("httpx.Client.get", lambda *_: httpx.Response(200))
    monkeypatch.setattr("webbrowser.open", opened.append)
    _open_when_ready("http://127.0.0.1:8000", threading.Event())
    assert opened == ["http://127.0.0.1:8000/ui/today"]
    stopped = threading.Event()
    stopped.set()
    _open_when_ready("http://127.0.0.1:8000", stopped)
    assert len(opened) == 1
