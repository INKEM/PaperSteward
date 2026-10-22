"""Explicit local launch: bind port, migrate, then serve. ASGI startup stays read-only."""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import webbrowser
from pathlib import Path
from tempfile import TemporaryFile

import httpx
import uvicorn
from alembic import command
from alembic.config import Config
from alembic.util.exc import CommandError
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from papersteward.api.app import create_app
from papersteward.config import Settings, format_settings_error
from papersteward.observability.logging import configure_logging
from papersteward.storage import create_db_engine
from papersteward.storage.migrations import find_alembic_ini


def add_command(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("start", help="迁移并启动本地服务")
    parser.add_argument("--data-dir", type=Path, help="工作区目录；显式指定时使用其中的独立数据库")
    parser.add_argument("--host", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, help="监听端口（默认 8000）")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")


def migrate(settings: Settings) -> None:
    config = Config(str(find_alembic_ini()))
    engine = create_db_engine(settings.db_url)
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
    finally:
        engine.dispose()


def _open_when_ready(url: str, stopped: threading.Event) -> None:
    with httpx.Client(trust_env=False, timeout=1) as client:
        for _ in range(75):
            if stopped.is_set():
                return
            try:
                response = client.get(url + "/health")
                if response.status_code == 200:
                    webbrowser.open(url + "/ui/today")
                    return
            except (httpx.HTTPError, OSError):
                pass
            if stopped.wait(0.2):
                return
    if not stopped.is_set():
        print(f"浏览器未自动打开，请手动访问 {url}/ui/today", file=sys.stderr)


def run(args: argparse.Namespace) -> int:
    options: dict[str, object] = {}
    if args.data_dir is not None:
        options.update(data_dir=args.data_dir, database_url=None)
    if args.host is not None:
        options["api_host"] = args.host
    if args.port is not None:
        options["api_port"] = args.port
    try:
        settings = Settings(**options)  # type: ignore[arg-type]
    except ValidationError as exc:
        print(format_settings_error(exc), file=sys.stderr)
        return 1
    configure_logging(settings)
    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with TemporaryFile(dir=settings.data_dir):
            pass
    except OSError:
        print("数据目录不可写，请检查权限或使用 --data-dir 指定新目录。", file=sys.stderr)
        return 1
    family = socket.AF_INET6 if ":" in settings.api_host else socket.AF_INET
    try:
        listener = socket.create_server((settings.api_host, settings.api_port), family=family)
    except OSError:
        print("端口被占用或监听地址不可用，请用 --port/--host 调整。", file=sys.stderr)
        return 1
    with listener:
        try:
            migrate(settings)
        except (OSError, SQLAlchemyError, CommandError):
            print(
                "迁移失败，请检查数据库权限与安装包；可运行 papersteward doctor 诊断。",
                file=sys.stderr,
            )
            return 1
        host = settings.api_host
        if host in {"0.0.0.0", "::"}:
            host = "127.0.0.1"
        elif ":" in host:
            host = f"[{host}]"
        url = f"http://{host}:{settings.api_port}"
        print(f"PaperSteward：{url}/ui/today\n按 Ctrl-C 停止服务，数据将保留。", flush=True)
        stopped = threading.Event()
        opener = None
        if not args.no_browser:
            opener = threading.Thread(target=_open_when_ready, args=(url, stopped), daemon=True)
            opener.start()
        try:
            server = uvicorn.Server(
                uvicorn.Config(
                    create_app(settings),
                    host=settings.api_host,
                    port=settings.api_port,
                    log_config=None,
                )
            )
            server.run(sockets=[listener])
        finally:
            stopped.set()
            if opener is not None:
                opener.join(timeout=2)
    return 0
