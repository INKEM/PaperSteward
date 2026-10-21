"""初始化 CLI：输入文件、可读审阅、分别标注与显式确认。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, urlencode

from papersteward.cli.onboarding_client import (
    MAX_BYTES,
    Client,
    ClientError,
    Kind,
    ReviewSnapshot,
    check_output,
    load_json,
    read_snapshot,
    save_snapshot,
    snapshot,
)

KINDS = ("profile", "taxonomy", "calibration", "dry-run")
GATES = dict(zip(KINDS, ("g1", "g2", "g3", "g4"), strict=True))
STAGES = dict(zip(KINDS, ("profile", "taxonomy", "calibration", "dry_run"), strict=True))
HINTS = {
    "seeds": "运行 onboarding start",
    "profile": "提交并审阅研究档案，再确认 G1",
    "taxonomy": "提交分类法与种子清单，审阅后确认 G2",
    "calibration": "创建校准轮次，双方分别标注后审阅并确认 G3",
    "dry_run": "提交参数/试运行日报，反馈认可后审阅并确认 G4",
    "active": "初始化完成；当前尚无真实发送器或调度器",
}


def add_commands(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    root = subparsers.add_parser("onboarding", help="初始化：提交、审阅、独立标注与 G1–G4 确认")
    root.add_argument(
        "--url", default="http://127.0.0.1:8000", help="本地 API 地址（放在子命令前）"
    )
    root.add_argument("--json", action="store_true", help="输出 JSON（放在子命令前）")
    commands = root.add_subparsers(dest="onboarding_command", required=True)
    for name, help_text in (
        ("status", "查看阶段和下一步"),
        ("start", "显式开始/恢复初始化"),
        ("import-seeds", "幂等导入 G2 已确认种子"),
    ):
        commands.add_parser(name, help=help_text)
    submit = commands.add_parser("submit", help="提交 Markdown 或 JSON，编辑须使用新版本")
    submit.add_argument("kind", choices=KINDS)
    submit.add_argument("--file", required=True, help="Markdown 或 JSON 输入文件")
    submit.add_argument("--id", dest="document_id", help="profile/taxonomy 文档 ID")
    submit.add_argument("--base-version", help="编辑文档的已有基线版本")
    submit.add_argument("--seeds", help="taxonomy 的独立种子 JSON 数组")
    submit.add_argument("--sample-kind", choices=("user", "synthetic"), help="taxonomy 种子来源")
    review = commands.add_parser("review", help="查看全文/差异/报告，保存后供显式确认使用")
    review.add_argument("kind", choices=KINDS)
    review.add_argument("--id", dest="document_id")
    review.add_argument(
        "--version", "--round", dest="version", required=True, help="文档/日报版本或校准轮号"
    )
    review.add_argument("--base-version", help="profile 差异比较的基线版本")
    label = commands.add_parser("label", help="分别提交 Agent/用户完整标注，不能覆盖")
    label.add_argument("role", choices=("agent", "user"))
    label.add_argument("--round", required=True)
    label.add_argument(
        "--file", required=True, help="独立批次 JSON；合成 fixture 可明确选择其角色批次"
    )
    feedback = commands.add_parser("feedback", help="针对已审日报追加认可/问题，不会通过 G4")
    feedback.add_argument("--review", required=True)
    feedback.add_argument("--by", required=True)
    choice = feedback.add_mutually_exclusive_group(required=True)
    choice.add_argument("--accept", action="store_true", help="已认可本版日报和参数")
    choice.add_argument(
        "--issue", action="append", choices=("too_many", "too_few", "inaccurate", "wrong_category")
    )
    feedback.add_argument("--comment")
    confirm = commands.add_parser("confirm", help="仅使用已审快照，不刷新依据；失败不会自动重试")
    confirm.add_argument("--gate", choices=("g1", "g2", "g3", "g4"), required=True)
    confirm.add_argument("--review", required=True)
    confirm.add_argument("--by", required=True)
    confirm.add_argument("--summary", required=True)
    confirm.add_argument(
        "--yes", action="store_true", required=True, help="已审阅全文/报告，明确确认此门槛"
    )
    confirm.add_argument("--accept-deviations", action="store_true", help="G3：明确接受指标偏差")
    confirm.add_argument("--known-biases", help="G3：书面已知偏差说明")
    for command in (submit, review, label, feedback):
        command.add_argument("--out", help="保存新的审阅快照（不覆盖文件）")


def _segment(value: Any) -> str:
    return quote(str(value), safe="")


def resource(kind: str, data: dict[str, Any]) -> str:
    if kind in {"profile", "taxonomy"}:
        return f"/onboarding/{kind}/{_segment(data['id'])}/{_segment(data['version'])}"
    if kind == "calibration":
        return f"/onboarding/calibration/{_segment(data['round_number'])}"
    return f"/onboarding/dry-runs/{_segment(data['version'])}"


def _read_markdown(path: str) -> str:
    with Path(path).open("rb") as source:
        raw = source.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ClientError("Markdown 超过 4 MiB；请缩短输入文件")
    return raw.decode("utf-8")


def _submit(args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    if args.kind in {"profile", "taxonomy"}:
        if not args.document_id:
            raise ClientError("提交文档需要 --id")
        body: dict[str, Any] = {
            "content_md": _read_markdown(args.file),
            f"{args.kind}_id": args.document_id,
            "base_version": args.base_version,
        }
        if args.kind == "taxonomy":
            if not args.seeds or not args.sample_kind:
                raise ClientError("分类法需要 --seeds 和 --sample-kind")
            body.update(seeds=load_json(args.seeds), sample_kind=args.sample_kind)
        return f"/onboarding/{args.kind}/drafts", body
    body = load_json(args.file)
    if not isinstance(body, dict):
        raise ClientError("提交文件必须是 JSON 对象")
    if args.kind == "calibration":
        # 合成 fixture 中两方答案永远不会随 cohort 创建请求提交。
        body = {k: v for k, v in body.items() if k not in {"agent_batch", "user_batch"}}
    return (
        "/onboarding/calibration/rounds" if args.kind == "calibration" else "/onboarding/dry-runs"
    ), body


def _confirmation(args: argparse.Namespace, value: ReviewSnapshot) -> tuple[str, dict[str, Any]]:
    if args.gate != GATES[value.kind] or value.stage.value != STAGES[value.kind]:
        raise ClientError("快照类型/阶段与 --gate 不匹配；请 review 当前待确认产物")
    if not args.by.strip() or not args.summary.strip():
        raise ClientError("确认者和变更摘要不能为空")
    body = {
        "confirmed": True,
        "confirmed_by": args.by,
        "change_summary": args.summary,
        "expected_revision": value.revision,
    }
    data = value.data
    for name in (
        ("content_hash",)
        if args.gate == "g1"
        else ("content_hash", "review_hash")
        if args.gate == "g2"
        else ("report_hash",)
        if args.gate == "g3"
        else ("report_hash", "feedback_hash")
    ):
        if not data.get(name):
            raise ClientError("审阅依据不完整；请先完成双方标注或日报认可，再 review")
        body[name] = data[name]
    if args.gate == "g3":
        if args.accept_deviations and not (args.known_biases or "").strip():
            raise ClientError("接受偏差需要 --known-biases 书面说明")
        body.update(accept_deviations=args.accept_deviations, known_biases=args.known_biases)
    elif args.accept_deviations or args.known_biases:
        raise ClientError("偏差接受参数只适用于 G3")
    return resource(value.kind, data) + "/confirm", body


def _terminal(value: str) -> str:
    return "".join(
        f"\\u{ord(char):04x}"
        if (ord(char) < 32 and char not in "\n\t") or 127 <= ord(char) <= 159
        else char
        for char in value
    )


def _display(value: dict[str, Any], json_output: bool) -> None:
    if json_output:
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return
    if "data" in value:
        data = dict(value["data"])
        for key in ("content_md", "markdown_diff"):
            if data.get(key):
                print(_terminal(data.pop(key)))
        report = data.get("report")
        if isinstance(report, dict) and report.get("markdown"):
            print(_terminal(report["markdown"]))
            data["report"] = {k: v for k, v in report.items() if k != "markdown"}
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2))
    stage = value.get("stage")
    if stage in HINTS:
        print("下一步：" + HINTS[stage])


def run(args: argparse.Namespace) -> int:
    try:
        client = Client(args.url)
        command = args.onboarding_command
        check_output(getattr(args, "out", None))
        if command in {"status", "start", "import-seeds"}:
            method, path = {
                "status": ("GET", "/onboarding"),
                "start": ("POST", "/onboarding/start"),
                "import-seeds": ("POST", "/onboarding/seeds/import"),
            }[command]
            _display(client.request(method, path), args.json)
            return 0
        if command in {"confirm", "feedback"}:
            value = read_snapshot(args.review, client)
            if command == "confirm":
                path, body = _confirmation(args, value)
                _display(client.request("POST", path, body), args.json)
                return 0
            if value.kind != "dry-run":
                raise ClientError("反馈需要 dry-run 审阅快照")
            result = client.request(
                "POST",
                resource(value.kind, value.data) + "/feedback",
                {
                    "report_hash": value.data["report_hash"],
                    "reviewed_by": args.by,
                    "accepted": args.accept,
                    "issues": args.issue or [],
                    "comment": args.comment,
                },
            )
            state = {"stage": value.stage.value, "revision": value.revision}
            kind: Kind = "dry-run"
        else:
            if command == "submit":
                path, body = _submit(args)
            elif command == "review":
                identity = {
                    "id": args.document_id,
                    "version": args.version,
                    "round_number": args.version,
                }
                if args.kind in {"profile", "taxonomy"} and not args.document_id:
                    raise ClientError("审阅文档需要 --id")
                path = resource(args.kind, identity)
                if args.base_version and args.kind == "profile":
                    path += "?" + urlencode({"base_version": args.base_version})
            else:
                body = load_json(args.file)
                if not isinstance(body, dict):
                    raise ClientError("标注批次必须是 JSON 对象")
                body = body.get(f"{args.role}_batch", body)
                path = f"/onboarding/calibration/{_segment(args.round)}/{args.role}-labels"
            state = client.request("GET", "/onboarding")
            result = (
                client.request("GET", path)
                if command == "review"
                else client.request("POST", path, body)
            )
            kind = cast(Kind, "calibration" if command == "label" else args.kind)
        reviewed = snapshot(client, kind, state, result)
        if args.out:
            save_snapshot(args.out, reviewed)
        _display(reviewed.model_dump(mode="json"), args.json)
        if args.out and not args.json:
            print(f"审阅已保存：{args.out}；请阅读后再运行 confirm --yes")
        return 0
    except (ClientError, OSError, ValueError, KeyError, TypeError) as exc:
        message = (
            str(exc)
            if isinstance(exc, ClientError)
            else "输入/审阅文件不完整或格式不合法；请检查路径和参数，重新 review"
        )
        print("错误：" + _terminal(message), file=sys.stderr)
        return 1
