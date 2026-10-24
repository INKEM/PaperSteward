"""队列操作与可恢复的运行进度。"""

from __future__ import annotations

from contextlib import suppress
from datetime import date, datetime
from html import escape
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from papersteward.api.deps import SessionDep
from papersteward.api.form_utils import PageError, conflict_redirect, failure, fields, hidden
from papersteward.api.html import HTML_HEADERS as HEADERS
from papersteward.api.html import page as _page
from papersteward.config import Settings
from papersteward.core.entities import Run, utc_now
from papersteward.core.enums import RunStatus
from papersteward.errors import NotFoundError
from papersteward.jobs.ui_flow import FlowRequest, processing_problem
from papersteward.pipeline.context import bound_context
from papersteward.scoring.queue import read_backlog
from papersteward.scoring.service import read_scoring_config
from papersteward.sources.contracts import describe_source_error, display_source_name
from papersteward.storage.mappers import as_utc
from papersteward.storage.onboarding import get_onboarding
from papersteward.storage.repositories import ProfileRepository, RunRepository

router = APIRouter(prefix="/ui", include_in_schema=False)
LABELS = {
    "queued": "排队",
    "collecting": "采集",
    "processing": "处理",
    "digest": "历史日报任务",
    "refreshing": "刷新队列",
    "ui.refresh": "刷新排序",
    "extracting": "生成研究草案",
    "completed": "完成",
    "running": "运行中",
    "succeeded": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
    "ui.sync": "挖掘新论文",
    "ui.digest": "历史日报任务",
    "ui.extract": "生成研究草案",
    "ingest": "来源采集",
}


def _run_form(kind: str, body: str, label: str, *, disabled: bool = False) -> str:
    return (
        '<form method="post" action="/ui/runs">'
        + hidden("run_id", uuid4().hex)
        + hidden("kind", kind)
        + body
        + f"<button{' disabled' if disabled else ''}>{escape(label)}</button></form>"
    )


def _number(values: dict[str, object], key: str) -> int:
    try:
        return max(0, int(str(values.get(key, 0))))
    except (TypeError, ValueError):
        return 0


def _timing(run: Run) -> str:
    """已用时；只在确有异常时补充卡住提示，不再重复展示刷新时刻。"""
    now = utc_now()
    elapsed = max(0, int(((run.finished_at or now) - run.started_at).total_seconds()))
    body = f'<p class="muted">已用时 {elapsed // 60} 分 {elapsed % 60} 秒</p>'
    if run.status is RunStatus.RUNNING:
        stats = dict(run.stats_json)
        updated = run.finished_at or run.started_at
        with suppress(ValueError, TypeError):
            updated = as_utc(datetime.fromisoformat(str(stats.get("updated_at"))))
        if max(0, int((now - updated).total_seconds())) > 300:
            body += (
                '<p class="notice">超过 5 分钟没有新进展，可能是模型响应过慢或网络中断。'
                "可停止任务，到「设置」页点「测试连接」检查后再重新开始。</p>"
            )
    return body


def run_body(
    run: Run,
    *,
    card: bool = False,
    current: Run | None = None,
    home: tuple[str, str] | None = None,
) -> str:
    """运行进度片段。home 为终端状态下唯一的返回链接（路径, 文案）。

    运行中唯一的动作是「停止任务」；ui.extract 的前进（审阅）由
    run_progress 在成功后自动跳转，不在这里渲染导航链接。
    """
    identity = quote(run.id, safe="")
    active = run.status is RunStatus.RUNNING
    path = f"/ui/current-run?identity={identity}" if card else f"/ui/runs/{identity}?fragment=true"
    poll = f' hx-get="{path}" hx-trigger="every 2s" hx-swap="outerHTML"' if active else ""
    stats = dict(run.stats_json)
    stage = str(stats.get("stage", "queued"))
    if run.run_type == "ingest" and active:
        # 来源采集 run 的 stats 没有 stage 键；运行中即视为采集中。
        stage = "collecting"
    total, processed = _number(stats, "processing_total"), _number(stats, "processed")
    index = _number(stats, "current_index") or min(processed + 1, total)
    kind = LABELS.get(run.run_type, run.run_type)
    if not active:
        title = kind + (
            "完成"
            if run.status is RunStatus.SUCCEEDED
            else LABELS.get(run.status.value, run.status.value)
        )
    else:
        phase = str(stats.get("processing_phase", ""))
        collecting_phase = str(stats.get("collecting_phase", ""))
        title = {
            "queued": "正在开始…",
            # 采集大步的两个子相位：检索规划（模型调用）与实际抓取。
            "collecting": "正在规划检索式" if collecting_phase == "planning" else "正在采集论文",
            "processing": (
                f"正在{'初筛排序' if phase == 'triage' else '深处理'}论文 {index} / {total}"
            ),
            "digest": "历史日报任务",
            "refreshing": "正在刷新阅读队列",
            "extracting": "正在生成研究草案",
            "completed": "正在保存运行结果",
        }.get(stage, "任务正在运行")
    body = f'<section class="run-progress" id="{"current-run" if card else "run-status"}"{poll}>'
    status_label = LABELS.get(run.status.value, run.status.value)
    body += f'<p class="muted">{escape(kind)} · {escape(status_label)}</p>'
    body += f'<h2 role="status">{escape(title)}</h2>'
    if run.run_type == "ui.sync":
        stages = ["queued", "collecting", "processing", "refreshing", "completed"]
    elif run.run_type == "ingest":
        stages = ["queued", "collecting", "completed"]
    else:
        stages = [
            "queued",
            "refreshing" if run.run_type == "ui.refresh" else "extracting",
            "completed",
        ]
    body += '<ol class="steps" aria-label="运行步骤">'
    for step in stages:
        attributes = ' class="current" aria-current="step"' if step == stage else ""
        label = "生成" if step in {"digest", "extracting"} else LABELS[step]
        body += f"<li{attributes}>{label}</li>"
    body += "</ol>"
    queued_note = stats.get("queued_note")
    if active and stage == "queued" and isinstance(queued_note, str) and queued_note:
        body += f'<p class="notice">{escape(queued_note)}</p>'
    extract_note = stats.get("extract_note")
    if active and stage == "extracting" and isinstance(extract_note, str) and extract_note:
        body += f"<p>{escape(extract_note)}</p>"
    if stage == "collecting":
        request = stats.get("request", {})
        if isinstance(request, dict):
            focus = str(request.get("focus", "")).strip()
            body += (
                f"<p>来源：{escape(str(request.get('source', '未知')))} · "
                f"检索式：Agent 按研究档案与已读历史自动生成"
                + (f" · 关注点：{escape(focus)}" if focus else "")
                + "</p>"
            )
    if total:
        percent = min(100, round(processed / total * 100))
        body += (
            f'<progress max="{total}" value="{min(processed, total)}" '
            f'aria-label="论文处理进度">{percent}%</progress>'
            f"<p>已处理 {processed} / {total} 篇（{percent}%） · "
            f"剩余 {max(0, total - processed)} 篇</p>"
        )
    elif active and stage != "queued":
        body += '<progress aria-label="任务进行中"></progress>'
    if stats.get("current_paper_title"):
        body += f"<p>当前论文：{escape(str(stats['current_paper_title']))}</p>"
    body += _timing(run)
    if active:
        caption = {
            "ui.sync": "停止会在当前这一批论文处理完后生效；已采集、已初筛、已处理的论文都会保留。",
            "ui.refresh": "停止后已保存的评分保留，排序可重新执行。",
            "ui.extract": (
                "停止会在本次模型调用结束后生效；上传的材料、修改意见和已生成的"
                "草案版本都保留，本次结果不保存。"
            ),
        }.get(run.run_type, "停止后已完成的数据保留。")
        body += (
            f'<form method="post" action="/ui/runs/{identity}/cancel" class="inline">'
            "<button>停止任务</button></form>"
            f'<p class="muted">{caption}</p>'
        )
    if not active:
        body += f"<p>最后阶段：{escape(LABELS.get(stage, stage))}</p>"
        if run.run_type == "ingest":
            # ingest run 的 stats 顶层就是采集字段；报告页展示来源统计与失败明细。
            filtered_raw = stats.get("filtered", [])
            filtered_count = (
                len(filtered_raw) if isinstance(filtered_raw, list) else _number(stats, "filtered")
            )
            body += (
                f"<p>候选 {_number(stats, 'candidates')} 篇 · "
                f"新增 {_number(stats, 'new')} · 已存在 {_number(stats, 'existing')} · "
                f"去重 {_number(stats, 'deduplicated')} · 过滤 {filtered_count} · "
                f"附带源版本 {_number(stats, 'source_versions_added')}</p>"
            )
            for item in stats.get("sources", []):
                if not isinstance(item, dict):
                    continue
                body += (
                    f"<p>{escape(display_source_name(str(item.get('source', ''))))}："
                    f"检索式成功 {item.get('queries_succeeded', 0)} 条 · "
                    f"候选 {item.get('candidates', 0)} 篇 · "
                    f"请求 {item.get('requests', 0)} 次"
                    + (
                        f" · 缓存命中 {item.get('cache_hits', 0)} 次"
                        if item.get("cache_hits")
                        else ""
                    )
                    + "</p>"
                )
        if run.run_type == "ui.sync":
            collected = stats.get("collected", {})
            collected = collected if isinstance(collected, dict) else {}
            filtered = collected.get("filtered", [])
            filtered_count = (
                len(filtered) if isinstance(filtered, list) else _number(collected, "filtered")
            )
            body += (
                f"<p>采集 {_number(collected, 'candidates')} 篇 · "
                f"新增 {_number(collected, 'new')} · 已存在 {_number(collected, 'existing')} · "
                f"去重 {_number(collected, 'deduplicated')} · 过滤 {filtered_count} · "
                f"深处理 {processed} 篇 · 自动归类 {_number(stats, 'auto_classified')} · "
                f"特殊桶 {_number(stats, 'auto_shelved')} · "
                f"跳过（无摘要）{_number(stats, 'skipped_no_summary')} 篇</p>"
            )
            queued_this_run = stats.get("queued_this_run")
            if isinstance(queued_this_run, int):
                target = _number(stats, "target")
                shortfall = _number(stats, "shortfall")
                line = f"<p>本次入队 {queued_this_run} 篇"
                if target:
                    line += f" / 期望 {target} 篇"
                if shortfall:
                    line += f"（差 {shortfall} 篇：候选已挖尽，宁缺毋滥）"
                line += "</p>"
                if _number(stats, "quota_filled"):
                    line += (
                        f"<p class='muted'>其中 {_number(stats, 'quota_filled')} 篇为保底填充："
                        "相关度未达正常门槛、按名次补足，详情页有标注。</p>"
                    )
                body += line
    # 轮询每 2s 整段替换本 section：details 带 hx-preserve 保留用户展开状态，
    # 终态（不再轮询/整页刷新）默认展开。
    preserve_attrs = " hx-preserve" if active else ""
    open_attrs = "" if active else " open"
    flags = stats.get("quality_flags", {})
    if isinstance(flags, dict) and flags:
        body += (
            f'<details id="run-quality-flags"{preserve_attrs}{open_attrs}>'
            "<summary>质量标记</summary><ul>"
            + "".join(
                f"<li>{escape(str(paper))}：{escape(' / '.join(map(str, values)))}</li>"
                for paper, values in flags.items()
                if isinstance(values, list)
            )
            + "</ul></details>"
        )
    plans = stats.get("plans")
    if run.run_type == "ui.sync" and isinstance(plans, list):
        items: list[str] = []
        for plan in plans:
            if not isinstance(plan, dict):
                continue
            note = "（模型规划不可用，档案兜底）" if plan.get("origin") == "profile" else ""
            for query in plan.get("queries") or []:
                if not isinstance(query, dict):
                    continue
                keywords = " ".join(str(word) for word in query.get("keywords", []))
                rationale = str(query.get("rationale", ""))
                items.append(
                    f"<li><code>{escape(keywords)}</code> — {escape(rationale)}{note}</li>"
                )
        if items:
            body += (
                f'<details id="run-query-plans"{preserve_attrs}{open_attrs}>'
                "<summary>本次使用的检索式（"
                + str(len(items))
                + " 条）</summary><ul>"
                + "".join(items)
                + "</ul></details>"
            )
    if "queue_total" in stats:
        body += (
            f'<p class="success">队列共 {_number(stats, "queue_total")} 篇 · '
            f"本次新增 {len(stats.get('added_ids', []))} 篇</p>"
        )
        if stats.get("queue_total") == 0:
            body += (
                "<p>暂无合格候选。Agent 下一轮会自动换角度生成检索式；也可以换个来源再挖一次。</p>"
            )
    if run.errors_json:
        messages = [
            str(error.get("message", "步骤失败，请检查设置后重试。"))
            for error in run.errors_json.get("errors", [])
            if isinstance(error, dict)
        ]
        if messages:
            body += (
                '<p role="alert">'
                + escape("；".join(messages))
                + '</p><a href="/ui/settings">检查模型与设置</a>'
            )
        source_failures = []
        for item in run.errors_json.get("sources", []):
            if not isinstance(item, dict):
                continue
            if item.get("stage") == "persist":
                source_failures.append(
                    "入库阶段失败：已采集数据未落库，原数据不受影响，可直接重试（pipeline_failed）"
                )
                continue
            code = str(item.get("code", ""))
            query_index = item.get("query_index")
            position = (
                f"第 {int(query_index) + 1} 条检索式：" if isinstance(query_index, int) else ""
            )
            source_failures.append(
                f"{display_source_name(str(item.get('source', '来源')))} {position}"
                f"{describe_source_error(code)}（{code}）"
            )
        if source_failures:
            body += (
                "<p>来源失败明细：</p><ul>"
                + "".join(f"<li>{escape(line)}</li>" for line in source_failures)
                + "</ul>"
            )
    if not active and run.run_type == "ui.sync":
        # 扩查每轮一份来源运行报告；逐轮可达（旧运行记录只有单个 id）。
        ingest_ids = stats.get("ingest_run_ids")
        links: list[str] = []
        if isinstance(ingest_ids, list) and ingest_ids:
            for round_no, round_ingest_id in enumerate(ingest_ids, 1):
                label = "查看来源运行报告" if len(ingest_ids) == 1 else f"第 {round_no} 轮报告"
                links.append(
                    f'<a href="/ui/runs/{quote(str(round_ingest_id), safe="")}">{label}</a>'
                )
        elif isinstance(stats.get("ingest_run_id"), str) and stats["ingest_run_id"]:
            links.append(
                f'<a href="/ui/runs/{quote(str(stats["ingest_run_id"]), safe="")}">'
                "查看来源运行报告</a>"
            )
        if links:
            body += f"<p>来源运行报告：{' · '.join(links)}</p>"
    if (
        run.run_type not in {"ui.digest", "ui.extract"}
        and run.status in (RunStatus.FAILED, RunStatus.CANCELLED)
        and isinstance(stats.get("request"), dict)
    ):
        payload = dict(stats["request"])
        retry_kind = str(payload.pop("kind", run.run_type.removeprefix("ui.")))
        body += _run_form(
            retry_kind,
            "".join(hidden(key, value) for key, value in payload.items()),
            "用原参数重试",
            disabled=current is not None and current.id != run.id,
        )
    if home is not None and not active:
        # 运行中唯一的动作是「停止任务」；返回链接只在终态出现。
        body += f'<p><a href="{escape(home[0])}">{escape(home[1])}</a></p>'
    return body + "</section>"


def queue_stats_line(session: SessionDep, unread_total: int, *, oob: bool = False) -> str:
    """队列顶部统计（UXR-05）：未读总数与阅读建议，口径不随筛选/已读查询变化。

    oob=True 时附带 htmx 带外更新属性，供完成/拒绝/恢复动作响应同步计数。
    """
    capacity = 5
    with suppress(IndexError, TypeError, ValueError, KeyError):
        profile = ProfileRepository(session).latest_confirmed()
        if profile is not None:
            capacity = int(profile.parsed_json["reading"]["weekly_capacity"])
    suggested = min(unread_total, max(1, (capacity + 6) // 7))
    oob_attr = ' hx-swap-oob="true"' if oob else ""
    return (
        f'<p id="queue-stats" class="queue-stats"{oob_attr}>'
        f"未读 <strong>{unread_total}</strong> 篇 · 按你的容量，今天建议先看 "
        f'<strong>{suggested}</strong> 篇。<span class="stats-note">'
        "全部论文会一直保留，随时可读。</span></p>"
    )


def dashboard(
    session: SessionDep, settings: Settings, day: date, *, unread_total: int = -1
) -> tuple[str, str]:
    """队列顶部（UXR-06）：返回 (标题行任务工具条, 仪表盘正文)。

    工具条由调用方放进 page() 的 heading_actions，与 h1 同行展示。
    """
    profile, _ = bound_context(
        session, utc_now(), action="ui.dashboard", enforce_schedule=False, day=day
    )
    repository = RunRepository(session)
    running = repository.running_ui_runs()
    busy = bool(running)
    if unread_total < 0:
        # 调用方未提供时自行读取；队列页已算出未读总数，传入可省一次查询。
        unread_total = read_backlog(session, now=utc_now(), limit=1).total
    problem = processing_problem(session, settings)
    scoring = read_scoring_config(session)
    default_target = int(scoring.gates.min_queue_yield) if scoring is not None else 5
    # 挖掘主按钮、折叠的采集参数、次级的刷新排序，一行排布。
    collect = (
        '<details class="collect-params"><summary>采集参数与检索说明</summary>'
        '<label>来源<select name="source">'
        + "".join(
            f'<option value="{value}">{label}</option>'
            for value, label in (
                ("arxiv", "arXiv"),
                ("openalex", "OpenAlex"),
                ("semantic_scholar", "Semantic Scholar"),
            )
        )
        + "</select></label>"
        + '<label>本次关注点（可选）<input type="text" name="focus" maxlength="500" '
        'placeholder="例如：本周想看评测方向的新基准"></label>'
        + f'<label>期望入队篇数<input type="number" name="target" value="{default_target}" '
        'min="1" max="50"></label>'
        # UXR-06：检索说明属于采集参数的语境，随面板折叠，不再常驻顶部。
         + '<p class="muted">不填检索词时由 Agent 依据研究档案、已读历史和关注点生成检索式'
        "（内容会发送给你配置的模型服务）。</p></details>"
    )
    disabled_sync = " disabled" if busy or problem else ""
    toolbar = (
        '<div class="queue-toolbar">'
        '<form method="post" action="/ui/runs" class="toolbar-sync">'
        + hidden("run_id", uuid4().hex)
        + hidden("kind", "sync")
        + f"<button{disabled_sync}>挖掘新论文</button>{collect}</form>"
        '<form method="post" action="/ui/runs" class="toolbar-refresh">'
        + hidden("run_id", uuid4().hex)
        + hidden("kind", "refresh")
        + f'<button class="secondary"{" disabled" if busy else ""}>刷新排序</button></form>'
        + "</div>"
    )
    body = run_body(running[0], card=True) if busy else ""
    body += '<section class="dashboard">'
    body += queue_stats_line(session, unread_total)
    if problem:
        body += f'<p class="notice">{escape(problem)} <a href="/ui/settings">连接模型</a></p>'
    elif busy:
        body += "<p>等待当前任务，完成后再开始新任务。</p>"
    elif not unread_total:
        body += "<p>点击「挖掘新论文」开始。采集、处理和评分完成后会直接加入队列。</p>"
    body += '<p class="muted">「刷新排序」只在本地计算，不调用模型。</p>'
    recent = repository.latest("ui.sync")
    if recent is not None:
        run = recent
        stats = dict(run.stats_json)
        collected = stats.get("collected", {})
        collected = collected if isinstance(collected, dict) else {}
        body += (
            f'<p>上一次挖掘：<a href="/ui/runs/{quote(run.id, safe="")}">'
            f"{escape(LABELS.get(run.status.value, run.status.value))}</a> · "
            f"采集新增 {_number(collected, 'new')} 篇 · "
            f"已存在 {_number(collected, 'existing')} 篇 · 处理 {_number(stats, 'processed')} 篇 · "
            f"队列新增 {len(stats.get('added_ids', []))} 篇</p>"
        )
    return toolbar, body + "</section>"


@router.post("/runs")
async def submit_run(request: Request) -> Response:
    try:
        values = await fields(request)
        spec = FlowRequest(
            run_id=values.get("run_id", ""),
            kind=values.get("kind"),
            source=values.get("source", "arxiv"),
            focus=values.get("focus", ""),
            target=values.get("target", "0"),
        )
        identity = request.app.state.ui_flows.submit(spec)
        return RedirectResponse("/ui/runs/" + identity, status_code=303)
    except PageError as exc:
        redirected = conflict_redirect(exc)
        if redirected is not None:
            return redirected
        return failure(exc, back="/ui/today")


@router.post("/runs/{identity}/cancel")
async def cancel_run(request: Request, identity: str, session: SessionDep) -> Response:
    """停止任务并回到当前阶段的页面；任务已结束时同样回退（幂等）。"""
    try:
        await fields(request)
        request.app.state.ui_flows.cancel(identity)
    except PageError as exc:
        return failure(exc)
    if get_onboarding(session).stage.value == "active":
        return RedirectResponse("/ui/today?cancelled=1", status_code=303)
    return RedirectResponse("/ui/setup", status_code=303)


@router.get("/current-run")
def current_run(identity: str, session: SessionDep) -> HTMLResponse:
    run = RunRepository(session).get(identity)
    if run is None or run.status is not RunStatus.RUNNING:
        return HTMLResponse("", headers={**HEADERS, "HX-Refresh": "true"})
    return HTMLResponse(run_body(run, card=True), headers=HEADERS)


@router.get("/runs/{identity}")
def run_progress(identity: str, session: SessionDep, fragment: bool = False) -> Response:
    repository = RunRepository(session)
    run = repository.get(identity)
    if run is None:
        raise NotFoundError("运行记录不存在")
    onboarding_active = get_onboarding(session).stage.value == "active"
    if run.run_type == "ui.extract" and run.status is RunStatus.SUCCEEDED and not onboarding_active:
        # 草案生成成功即单向往前：直接进入设置页的审阅界面。
        if fragment:
            return HTMLResponse(
                '<p class="success">草案已生成，正在打开审阅…</p>',
                headers={**HEADERS, "HX-Redirect": "/ui/setup"},
            )
        return RedirectResponse("/ui/setup", status_code=303)
    running = repository.running_ui_runs()
    home = ("/ui/today", "返回队列") if onboarding_active else ("/ui/setup", "返回首次设置")
    body = run_body(run, current=running[0] if running else None, home=home)
    return (
        HTMLResponse(body, headers=HEADERS) if fragment else _page("运行进度", body, nav="阅读队列")
    )
