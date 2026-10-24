"""性能基线：种子 N 篇可入队论文后，计量队列读取/页面渲染/刷新排序/挖掘干跑的 SQL 条数与耗时。

阶段 2 各项改造用同一脚本对比前后数据（详见
dev-docs/PaperSteward_质量与体验分阶段改进计划.md 的 H-04）。

用法::

    .venv/bin/python scripts/perf_baseline.py                     # 默认 --papers 100 500
    .venv/bin/python scripts/perf_baseline.py --papers 1000
    .venv/bin/python scripts/perf_baseline.py --out var/perf/after-e01.json --label after-e01

输出写入 JSON（默认 var/perf/baseline.json）；目标文件已存在时先打印与它的差值再覆盖。
依赖 tests/integration 的激活与种子工具（与集成测试同一套夹具，不走真实网络）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "tests" / "integration"))

from alembic import command  # noqa: E402
from alembic.config import Config as AlembicConfig  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from test_priority_queue import seed  # noqa: E402
from test_ui_flows import (  # noqa: E402
    ABSTRACT,
    PROCESS_FIXTURES,
    RoutingFakeLLM,
    canned_plan,
    relevance_payload,
    wait,
)

from conftest import activate_workspace  # noqa: E402


def isolate_env() -> None:
    for key in list(os.environ):
        if key.startswith("PAPERSTEWARD_"):
            del os.environ[key]
    os.environ["PAPERSTEWARD_LLM_PROVIDER"] = "fake"
    os.environ["PAPERSTEWARD_LLM_BASE_URL"] = "https://api.openai.com/v1"
    os.environ["PAPERSTEWARD_LLM_MODEL"] = ""
    os.environ["PAPERSTEWARD_LLM_API_KEY"] = ""


def make_engine() -> Engine:
    from papersteward.storage import create_db_engine

    tmp = Path(tempfile.mkdtemp(prefix="ps-perf-", dir="/dev/shm"))
    engine = create_db_engine(f"sqlite+pysqlite:///{tmp / 'perf.db'}")
    config = AlembicConfig(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    return engine


class SQLCounter:
    """在 with 块内统计引擎收到的 SQL 语句条数。"""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.count = 0

    def _inc(self, *args: Any, **kwargs: Any) -> None:
        self.count += 1

    def __enter__(self) -> SQLCounter:
        event.listen(self.engine, "before_cursor_execute", self._inc)
        return self

    def __exit__(self, *exc: Any) -> None:
        event.remove(self.engine, "before_cursor_execute", self._inc)


def timed(engine: Engine, fn: Any) -> dict[str, Any]:
    with SQLCounter(engine) as counter:
        start = time.perf_counter()
        result = fn()
        elapsed_ms = (time.perf_counter() - start) * 1000
    return {"sql": counter.count, "ms": round(elapsed_ms, 1), "result": result}


def bench_read_paths(engine: Engine, papers: int) -> dict[str, Any]:
    from papersteward.api import create_app
    from papersteward.scoring.queue import read_backlog, refresh_queue
    from papersteward.storage import create_session_factory, session_scope

    factory = create_session_factory(engine)
    with session_scope(factory) as session:
        for index in range(papers):
            seed(session, f"perf-{index:05d}", r=55 + (index * 7) % 40)

    metrics: dict[str, Any] = {"papers_seeded": papers}

    # 先计量一次完整刷新（快照创建 + 入队 + after 全量读取）。
    def _refresh() -> Any:
        with session_scope(factory) as session:
            return refresh_queue(session, now=_now()).model_dump()

    metrics["refresh_queue_first"] = timed(engine, _refresh)
    metrics["refresh_queue_first"].pop("result")

    def _backlog() -> Any:
        with session_scope(factory) as session:
            page = read_backlog(session, now=_now(), limit=50)
            return {"total": page.total, "shown": len(page.items)}

    metrics["read_backlog_50"] = timed(engine, _backlog)

    with TestClient(create_app(engine=engine)) as client:
        metrics["ui_queue_page"] = timed(engine, lambda: client.get("/ui/queue"))
        metrics["ui_queue_page"]["status"] = metrics["ui_queue_page"]["result"].status_code
        metrics["ui_queue_page"].pop("result")
        # UXR-24：归档与排序视图也是日常读路径，纳入基线（无归档记录时页面走空态）。
        for path, key in (("/ui/archive", "ui_archive_page"), ("/ui/views", "ui_views_page")):
            metrics[key] = timed(engine, lambda p=path: client.get(p))
            metrics[key]["status"] = metrics[key]["result"].status_code
            metrics[key].pop("result")
    return metrics


def _now() -> Any:
    from papersteward.core.entities import utc_now

    return utc_now()


def bench_sync_dry_run(engine: Engine, screened: int = 5) -> dict[str, Any]:
    """fake LLM 下一次 ui.sync 全流程的墙钟与 SQL（挖掘管线固定开销）。"""
    import papersteward.jobs.ui_flow as ui_flow
    from papersteward.config import get_settings
    from papersteward.core.entities import Paper, utc_now
    from papersteward.core.enums import PaperStatus
    from papersteward.storage import create_session_factory, session_scope
    from papersteward.storage.repositories import PaperRepository

    activate_workspace(engine)
    factory = create_session_factory(engine)
    with session_scope(factory) as session:
        from papersteward.llm.runtime import LLMSettingsPayload, save_llm_config

        save_llm_config(
            session,
            LLMSettingsPayload(
                base_url="https://api.example.com/v1",
                api_key="sk-test",
                model="test-model",
                consent=True,
            ),
            get_settings(),
        )
        for index in range(screened):
            paper_id = f"dry-{index:03d}"
            if PaperRepository(session).get(paper_id) is None:
                PaperRepository(session).add(
                    Paper(
                        paper_id,
                        f"Dry run paper {index}",
                        abstract=ABSTRACT,
                        status=PaperStatus.SCREENED,
                        first_seen_at=utc_now(),
                    )
                )

    original_provider = ui_flow.build_llm_provider
    original_plan = ui_flow.UIFlowManager._plan
    original_collect = ui_flow.UIFlowManager.collect
    payloads = {**PROCESS_FIXTURES, "relevance": relevance_payload(4)}
    ui_flow.build_llm_provider = lambda config: RoutingFakeLLM(payloads)
    manager = ui_flow.UIFlowManager(factory, get_settings())
    manager._plan = canned_plan()  # type: ignore[method-assign]
    manager.collect = lambda request, plan: None  # type: ignore[method-assign]
    try:
        identity = "perf-" + uuid4().hex[:12]

        def _run() -> Any:
            manager.submit(ui_flow.FlowRequest(run_id=identity, kind="sync", target=str(screened)))
            return wait(manager, identity).status.value

        result = timed(engine, _run)
        return {"screened": screened, "ui_sync_dry_run": dict(result)}
    finally:
        manager.close()
        ui_flow.build_llm_provider = original_provider
        ui_flow.UIFlowManager._plan = original_plan
        ui_flow.UIFlowManager.collect = original_collect


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--papers", type=int, nargs="+", default=[100, 500])
    parser.add_argument("--sync", action="store_true", help="附带一次 fake LLM 挖掘干跑")
    parser.add_argument("--label", default="run")
    parser.add_argument("--out", default="var/perf/baseline.json")
    args = parser.parse_args()

    isolate_env()
    report: dict[str, Any] = {"label": args.label, "created": time.strftime("%Y-%m-%dT%H:%M:%S")}
    for papers in args.papers:
        engine = make_engine()
        try:
            activate_workspace(engine)
            report[f"papers_{papers}"] = bench_read_paths(engine, papers)
        finally:
            engine.dispose()
            shutil.rmtree(Path(engine.url.database).parent, ignore_errors=True)
    if args.sync:
        engine = make_engine()
        try:
            report["sync"] = bench_sync_dry_run(engine)
        finally:
            engine.dispose()
            shutil.rmtree(Path(engine.url.database).parent, ignore_errors=True)

    out_path = Path(args.out)
    if out_path.exists():
        try:
            previous = json.loads(out_path.read_text(encoding="utf-8"))
            print(f"—— 与既有基线 {args.out}（label={previous.get('label')}）对比 ——")
            for section, metrics in report.items():
                if not isinstance(metrics, dict):
                    continue
                for name, value in metrics.items():
                    if not isinstance(value, dict) or "sql" not in value:
                        continue
                    old = previous.get(section, {}).get(name, {})
                    if "sql" in old:
                        print(
                            f"{section}.{name}: SQL {old['sql']} -> {value['sql']}"
                            f"（{value['sql'] - old['sql']:+d}）"
                            f" · {old['ms']}ms -> {value['ms']}ms"
                        )
        except (json.JSONDecodeError, OSError):
            print("（旧基线文件不可读，跳过对比）")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"—— 已写入 {out_path} ——")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
