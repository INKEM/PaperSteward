"""采集与运行报告 CLI；fixture使用注入transport，live必须明确选择。"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

import httpx
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from papersteward.config import Settings, format_settings_error
from papersteward.core.enums import RunStatus
from papersteward.core.errors import DomainError
from papersteward.errors import AppError
from papersteward.pipeline.ingest import ingest
from papersteward.sources.arxiv import ArxivAdapter
from papersteward.sources.contracts import QuerySpec, SourceAdapter
from papersteward.sources.fixtures import FakeSourceAdapter
from papersteward.sources.http import SourceHTTP
from papersteward.sources.openalex import OpenAlexAdapter
from papersteward.sources.semantic_scholar import SemanticScholarAdapter
from papersteward.storage import create_db_engine, create_session_factory, session_scope
from papersteward.storage.repositories import RunRepository

SOURCES = ("arxiv", "openalex", "semantic_scholar", "fake")


def _fixture_transport(path: Path) -> httpx.MockTransport:
    """原始响应文件重复回放；`{"responses": [...]}` 按请求顺序回放多页响应。"""
    body = path.read_bytes()
    try:
        cassette = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        cassette = None
    if not (isinstance(cassette, dict) and isinstance(cassette.get("responses"), list)):
        return httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    responses: list[bytes] = []
    for item in cassette["responses"]:
        if isinstance(item, str):
            responses.append(item.encode("utf-8"))
        elif isinstance(item, (dict, list)):
            responses.append(json.dumps(item, ensure_ascii=False).encode("utf-8"))
        else:
            raise ValueError("fixture responses 只支持对象、数组或字符串")
    if not responses:
        raise ValueError("fixture responses 不能为空")
    pending = iter(responses)

    def handle(request: httpx.Request) -> httpx.Response:
        try:
            content = next(pending)
        except StopIteration:
            # 不重复最后一页，避免把分页耗尽伪装成可解析的重复候选。
            return httpx.Response(400, content=b'{"error":"fixture responses exhausted"}')
        return httpx.Response(200, content=content)

    return httpx.MockTransport(handle)


def add_commands(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    command = subparsers.add_parser("ingest", help="采集候选并去重/硬过滤；需完成G4")
    command.add_argument("--source", choices=SOURCES, action="append", required=True)
    command.add_argument(
        "--fixture", action="append", default=[], help="单来源路径或 SOURCE=PATH，可重复"
    )
    command.add_argument("--live", action="store_true", help="明确允许所选来源真实HTTP请求")
    command.add_argument("--query", help="单个字面关键词/短语；省略则从已选档案生成")
    command.add_argument("--max-results", type=int, default=100)
    command.add_argument("--dry-run", action="store_true", help="仅预览，不写数据库")
    command.add_argument("--run-id", default=None)
    command.add_argument("--json", action="store_true", help="报告始终以JSON输出")
    runs = subparsers.add_parser("runs", help="查看运行报告")
    show = runs.add_subparsers(dest="runs_command", required=True).add_parser("show")
    show.add_argument("run_id", nargs="?")
    show.add_argument("--last", action="store_true")


def _adapters(
    args: argparse.Namespace, settings: Settings, clients: list[SourceHTTP]
) -> list[SourceAdapter]:
    fixtures: dict[str, Path] = {}
    for value in args.fixture:
        if "=" in value:
            source, path = value.split("=", 1)
        elif len(args.source) == 1:
            source, path = args.source[0], value
        else:
            raise ValueError("多来源fixture须用SOURCE=PATH")
        if source not in args.source or source in fixtures:
            raise ValueError("fixture来源未知或重复")
        fixtures[source] = Path(path)
    if len(set(args.source)) != len(args.source):
        raise ValueError("来源不能重复")
    result: list[SourceAdapter] = []
    for source in args.source:
        path = fixtures.get(source)
        if source == "fake":
            if path is None:
                raise ValueError("fake来源必须提供fixture")
            result.append(FakeSourceAdapter(path))
            continue
        if path is None and not args.live:
            raise ValueError("请提供--fixture；真实HTTP调用须显式--live")
        transport = _fixture_transport(path) if path else None
        client = SourceHTTP(
            source,
            transport=transport,
            min_interval=0 if path else 3,
            api_key=settings.semantic_scholar_api_key
            if source == "semantic_scholar" and not path
            else None,
        )
        clients.append(client)
        factory = {
            "arxiv": ArxivAdapter,
            "openalex": OpenAlexAdapter,
            "semantic_scholar": SemanticScholarAdapter,
        }[source]
        result.append(factory(client))
    return result


def run(args: argparse.Namespace) -> int:
    clients: list[SourceHTTP] = []
    engine = None
    try:
        settings = Settings()
        engine = create_db_engine(settings.db_url)
        with session_scope(create_session_factory(engine)) as session:
            if args.command == "runs":
                if args.last == bool(args.run_id):
                    raise ValueError("请选择运行ID或--last之一")
                repo = RunRepository(session)
                values = repo.list(limit=1) if args.last else []
                report = values[0] if values else repo.get(args.run_id) if args.run_id else None
                if report is None:
                    raise ValueError("未找到运行记录")
            else:
                adapters = _adapters(args, settings, clients)
                queries = (
                    [QuerySpec(keywords=(args.query,), max_results=args.max_results)]
                    if args.query
                    else None
                )
                if not args.query and args.max_results != 100:
                    from papersteward.sources.queries import generate_queries, selected_profile

                    queries = generate_queries(
                        selected_profile(session), max_results=args.max_results
                    )
                report = ingest(
                    session,
                    adapters,
                    run_id=args.run_id or uuid4().hex,
                    queries=queries,
                    dry_run=args.dry_run,
                )
        print(json.dumps(asdict(report), ensure_ascii=False, indent=2, default=str))
        return int(report.status is RunStatus.FAILED)
    except ValidationError as exc:
        # 配置/输入错误不回显原始值（尤其是密钥）。
        print(
            format_settings_error(exc) if engine is None else "输入校验失败，请检查查询/过滤配置",
            file=sys.stderr,
        )
    except (DomainError, AppError, ValueError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
    except SQLAlchemyError:
        print("数据库未迁移或不可用，请运行 alembic upgrade head 后重试", file=sys.stderr)
    finally:
        for client in clients:
            client.close()
        if engine is not None:
            engine.dispose()
    return 1
