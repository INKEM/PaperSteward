"""G3/G4 browser forms over the frozen calibration and dry-run services."""

from __future__ import annotations

import json
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select

from papersteward.api import onboarding as api
from papersteward.api.deps import SessionDep
from papersteward.api.setup_pages import (
    approval,
    confirmation_fields,
    form,
    hidden,
    input_field,
    starter,
    textarea,
)
from papersteward.errors import InvalidRequestError
from papersteward.onboarding.calibration import AgentBatch, UserBatch
from papersteward.onboarding.calibration_service import create_round, submit_labels
from papersteward.onboarding.dry_run import DryRunFeedback, DryRunRequest
from papersteward.onboarding.dry_run_service import _body, _context, _latest_version
from papersteward.storage.models import CalibrationRoundRow
from papersteward.storage.onboarding import get_onboarding

NOTICE = '<p class="notice">示例/合成资料只验证流程，不代表真实模型质量或用户标注。</p>'


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def calibration_page(session: SessionDep) -> str:
    rows = session.scalars(
        select(CalibrationRoundRow).order_by(CalibrationRoundRow.round_number)
    ).all()
    body = NOTICE
    if not rows:
        body += form("calibration/example", "", "使用示例生成校准报告")
    else:
        number = rows[-1].round_number
        view = api.review_calibration(number, session)
        report = view["report"] or {}
        body += '<article><h2>校准报告</h2><dl class="metrics">'
        for key, label in (
            ("spearman", "排序相关"),
            ("top5_overlap", "前五重合"),
            ("primary_tag_agreement", "主标签一致率"),
            ("read_mode_agreement", "阅读方式一致率"),
        ):
            raw = report.get(key)
            value = (
                f"{raw:.3f}"
                if isinstance(raw, float)
                else str(raw if raw is not None else "待提交标注")
            )
            body += f"<div><dt>{label}</dt><dd>{escape(value)}</dd></div>"
        body += '</dl><p class="notice">' + (
            "指标通过，仍需审阅完整报告并明确确认。"
            if report.get("metrics_passed")
            else "指标尚未通过，请检查报告、双方标注或明确记录已知偏差。"
        )
        body += (
            "</p><details><summary>完整指标、分歧与建议</summary><pre>"
            + escape(_json(view))
            + "</pre></details></article>"
        )
        if view["report"] is not None:
            fields = confirmation_fields(view, get_onboarding(session).revision)
            fields += hidden("round_number", number)
            fields += "<details><summary>知悉偏差后接受（指标未达标时）</summary>"
            fields += (
                '<label><input type="checkbox" name="accept_deviations" value="yes">'
                "我知悉并接受指标偏差</label>"
            )
            fields += (
                '<label>已知偏差说明<textarea name="known_biases"></textarea></label></details>'
            )
            body += form("calibration/confirm", fields, "确认 G3")
        for role, submitted in (
            ("agent", view["agent_submitted"]),
            ("user", view["user_submitted"]),
        ):
            if not submitted:
                fields = hidden("round_number", number)
                fields += (
                    textarea("payload", "{}", f"{role} 独立标注 JSON")
                    .replace('id="payload"', f'id="{role}-payload"')
                    .replace('for="payload"', f'for="{role}-payload"')
                )
                body += "<details><summary>提交 " + role + " 标注</summary>"
                body += form("calibration/" + role, fields, "提交本方标注") + "</details>"
    if not rows or (rows[-1].round_number == 1 and rows[-1].report_json is not None):
        cohort = json.loads(starter("calibration.json"))
        cohort.pop("agent_batch")
        cohort.pop("user_batch")
        cohort["round_number"] = 1 if not rows else 2
        custom = (
            '<label>导入校准 JSON<input type="file" accept=".json" '
            'data-import-target="calibration"></label>'
        )
        custom += textarea("payload", _json(cohort), "校准样本 JSON")
        # The DOM id is distinct from the label-upload forms.
        custom = custom.replace('id="payload"', 'id="calibration"').replace(
            'for="payload"', 'for="calibration"'
        )
        body += "<details><summary>高级：上传自己的校准样本，再分别提交双方标注</summary>"
        body += form("calibration/create", custom, "创建校准轮次") + "</details>"
    return body


def dry_run_page(session: SessionDep) -> str:
    version = _latest_version(session)
    draft = json.loads(starter("dry_run.json"))
    body = NOTICE
    if version:
        view = api.review_dry_run(version, session)
        previous, _ = _body(session, version)
        draft = previous.model_dump(mode="json")
        body += (
            "<article><h2>试运行日报预览</h2><pre>"
            + escape(view["report"]["markdown"])
            + "</pre></article>"
        )
        review_fields = hidden("version", version) + hidden("report_hash", view["report_hash"])
        review_fields += input_field("reviewed_by", "local-user", "审阅人")
        body += form(
            "dry-run/feedback", review_fields + hidden("accepted", "yes"), "我认可这份试运行日报"
        )
        feedback = review_fields + '<label>需要调整<select name="issue">'
        for key, label in (
            ("too_many", "太多"),
            ("too_few", "太少"),
            ("inaccurate", "不准"),
            ("wrong_category", "分类不对"),
        ):
            feedback += f'<option value="{key}">{label}</option>'
        feedback += '</select></label><label>说明<textarea name="comment"></textarea></label>'
        body += (
            "<details><summary>提出调整意见</summary>"
            + form("dry-run/feedback", feedback, "记录意见")
            + "</details>"
        )
        if view["feedback"] and view["feedback"]["accepted"]:
            confirm = confirmation_fields(view, get_onboarding(session).revision)
            body += form(
                "dry-run/confirm", hidden("version", version) + confirm, "确认 G4 并完成设置"
            )
        if view["feedback_history"]:
            body += (
                "<details><summary>反馈历史</summary><pre>"
                + escape(_json(view["feedback_history"]))
                + "</pre></details>"
            )
    _, codes = _context(session)
    draft["version"] = version + 1
    parameters = draft["parameters"]
    if not version:
        parameters["target_shares"] = {code: 1 / len(codes) for code in sorted(codes)}
    editor = hidden("version", version + 1) + hidden("baseline", _json(draft))
    editor += input_field("push_time", parameters["push_time"], "计划阅读时间（HH:MM）")
    editor += input_field("timezone", parameters["timezone"], "时区")
    editor += (
        '<label>安静时段开始（可留空）<input name="quiet_start" value="'
        + escape(parameters["quiet_start"] or "")
        + '"></label>'
    )
    editor += (
        '<label>安静时段结束（可留空）<input name="quiet_end" value="'
        + escape(parameters["quiet_end"] or "")
        + '"></label>'
    )
    editor += (
        '<label class="check"><input type="checkbox" name="paused" value="yes"'
        + (" checked" if parameters["paused"] else "")
        + ">暂停日常处理</label>"
    )
    editor += '<label>外部模型的数据授权<select name="external_model_data">'
    for value, label in (
        ("disabled", "仅离线体验，不授权外发"),
        ("metadata_abstract", "允许将论文元数据和摘要发给配置的模型"),
    ):
        selected = " selected" if parameters["external_model_data"] == value else ""
        editor += f'<option value="{value}"{selected}>{label}</option>'
    editor += (
        "</select></label><p>备注不外发。此处确认数据范围，配置模型后仍需主动点击同步/处理。</p>"
    )
    editor += textarea(
        "target_shares", _json(parameters["target_shares"]), "目标主标签阅读占比（总和为 1）"
    )
    editor += "<p>默认每天主队列最多7篇、精读2篇、粗读5篇；本地阅读，不自动发送。</p>"
    body += "<h2>运行参数</h2>" + form("dry-run/create", editor, "生成新的试运行预览")
    advanced = textarea("payload", _json(draft), "完整试运行 JSON（可调整配额、条目和配置）")
    advanced = advanced.replace('id="payload"', 'id="dry-run-json"').replace(
        'for="payload"', 'for="dry-run-json"'
    )
    advanced = (
        '<label>导入 JSON<input type="file" accept=".json" '
        'data-import-target="dry-run-json"></label>' + advanced
    )
    body += (
        "<details><summary>高级参数与自定义试运行条目</summary>"
        + form("dry-run/advanced", advanced, "校验并预览")
        + "</details>"
    )
    return body


def advance_action(kind: str, action: str, data: dict[str, str], session: SessionDep) -> Response:
    if kind == "calibration":
        if action == "example":
            example = json.loads(starter("calibration.json"))
            agent = AgentBatch.model_validate(example.pop("agent_batch"))
            user = UserBatch.model_validate(example.pop("user_batch"))
            body = api.CreateCalibrationRequest.model_validate(example)
            with api._onboarding_errors(), session.begin():
                timestamp = api.utc_now()
                create_round(
                    session,
                    body.round_number,
                    body.papers,
                    sample_kind=body.sample_kind,
                    rubric_version=body.rubric_version,
                    occurred_at=timestamp,
                )
                submit_labels(session, body.round_number, agent, occurred_at=timestamp)
                submit_labels(session, body.round_number, user, occurred_at=timestamp)
        elif action == "create":
            api.create_calibration_round(
                api.CreateCalibrationRequest.model_validate_json(data.get("payload", "")), session
            )
        elif action == "agent":
            api.submit_agent_labels(
                int(data.get("round_number", "0")),
                AgentBatch.model_validate_json(data.get("payload", "")),
                session,
            )
        elif action == "user":
            api.submit_user_labels(
                int(data.get("round_number", "0")),
                UserBatch.model_validate_json(data.get("payload", "")),
                session,
            )
        elif action == "confirm":
            body_confirm = api.ConfirmCalibrationRequest(
                **approval(data),
                report_hash=data.get("report_hash", ""),
                accept_deviations=data.get("accept_deviations") == "yes",
                known_biases=data.get("known_biases") or None,
            )
            api.confirm_calibration_g3(int(data.get("round_number", "0")), body_confirm, session)
        else:
            raise InvalidRequestError("未知校准操作")
    elif kind == "dry-run":
        version = int(data.get("version", "1"))
        if action == "create":
            example = json.loads(data.get("baseline") or starter("dry_run.json"))
            example["version"] = version
            parameters = example["parameters"]
            parameters.update(
                push_time=data.get("push_time", ""),
                timezone=data.get("timezone", ""),
                quiet_start=data.get("quiet_start") or None,
                quiet_end=data.get("quiet_end") or None,
                paused=data.get("paused") == "yes",
                external_model_data=data.get("external_model_data", "disabled"),
                target_shares=json.loads(data.get("target_shares", "")),
            )
            DryRunRequest.model_validate(example)
            example["digest_date"] = (
                api.utc_now().astimezone(ZoneInfo(parameters["timezone"])).date().isoformat()
            )
            api.create_dry_run(DryRunRequest.model_validate(example), session)
        elif action == "advanced":
            api.create_dry_run(DryRunRequest.model_validate_json(data.get("payload", "")), session)
        elif action == "feedback":
            feedback = DryRunFeedback(
                report_hash=data.get("report_hash", ""),
                reviewed_by=data.get("reviewed_by", ""),
                accepted=data.get("accepted") == "yes",
                issues=[data["issue"]] if data.get("issue") else [],
                comment=data.get("comment") or None,
            )
            api.submit_dry_run_feedback(version, feedback, session)
        elif action == "confirm":
            api.confirm_dry_run_g4(
                version,
                api.ConfirmDryRunRequest(
                    **approval(data),
                    report_hash=data.get("report_hash", ""),
                    feedback_hash=data.get("feedback_hash", ""),
                ),
                session,
            )
        else:
            raise InvalidRequestError("未知试运行操作")
    return RedirectResponse("/ui/setup", status_code=303)
