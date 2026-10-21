# 初始化 CLI 操作指南

`papersteward onboarding` 是本地 API 客户端。先启动服务器，再逐步提交、审阅和确认。
不自动迁移、不调用真实模型、不启动推送。最小体验采用 CLI；浏览器可使用 `/docs`。

```bash
.venv/bin/alembic upgrade head
.venv/bin/uvicorn papersteward.main:app --host 127.0.0.1 --port 8000
```

以上命令使用当前配置的数据库，已有数据应先备份。仅演示时可先设置
`PAPERSTEWARD_DATABASE_URL` 指向独立临时 SQLite；不要用演示答案初始化真实研究档案。
本环境普通文件系统的 SQLite fsync 会挂起，测试使用 `/dev/shm` 临时库；普通环境
可照常使用 `.env` 配置。CLI 不读服务器数据库，服务器和 CLI 可在两个终端运行。

## 命令约定

```bash
.venv/bin/papersteward onboarding --help
.venv/bin/papersteward onboarding status
.venv/bin/papersteward onboarding --url http://127.0.0.1:8000 --json status
```

`--url`、`--json` 放在操作子命令前。只支持 `127.0.0.1` / `localhost` / `::1`，
不使用环境代理，不跟随重定向。默认地址是 `http://127.0.0.1:8000`，请求超时 10 秒。

- `submit` 保存草案/校准轮次/日报，编辑文档须提升正文版本并提供 `--base-version`。
- `review` 展示完整正文、差异、缺项/协商问题或报告；`--out` 保存供确认使用的快照。
- `label agent` 与 `label user` 分别提交完整批次，不生成答案，不会相互代填。
- `feedback` 记录日报意见，`--accept` 明确认可；反馈不会自动通过 G4。
- `confirm` 必须提供 `--gate`、`--review`、`--by`、`--summary` 和 `--yes`。
  `--yes` 表示已经审阅本次全文/报告并明确确认，不会自动读取较新的依据。

`--out` 是包含全文、参数/报告、服务器、阶段/revision 与校验和的 JSON 快照，权限
0600，拒绝覆盖已有文件。**编辑输入文件，勿编辑审阅快照**；校验和用于发现意外修改，
不是签名/鉴权。服务器地址必须一致；即使 localhost 和 127.0.0.1 指向同机，也应保持
相同 `--url`。快照包含用户数据，默认建议保存在已忽略的 `var/` 内，不提交版本库。

## 合成数据的完整手动演示

以下使用测试 fixture，全部结果仅流程验证。每次确认前阅读终端输出；真实校准必须
用双方独立答案，不能复制下列 fixture。为演示选择独立空库，不要重置已有 ACTIVE 库。

先准备独立的种子清单与审阅目录；重复演示请换目录/数据库，快照不会被覆盖。

```bash
mkdir -p var/onboarding-review
.venv/bin/python - <<'PY'
import json
from pathlib import Path
seeds = [
    {"paper_id": f"syn-T{tag:02}-{i}", "title": f"合成 {tag}/{i}", "primary_tag": f"T{tag:02}"}
    for tag in range(1, 6) for i in range(1, 5)
]
Path("var/onboarding-review/seeds.json").write_text(json.dumps(seeds, ensure_ascii=False), encoding="utf-8")
PY
.venv/bin/papersteward onboarding start
.venv/bin/papersteward onboarding submit profile --id demo-profile \
  --file tests/fixtures/profiles/basic.md --out var/onboarding-review/g1.json
```

阅读档案正文、G1 缺项/问题；完整且认同时再确认：

```bash
.venv/bin/papersteward onboarding confirm --gate g1 --review var/onboarding-review/g1.json \
  --by demo-user --summary '已审阅合成档案' --yes
.venv/bin/papersteward onboarding submit taxonomy --id onboarding-taxonomy \
  --file tests/fixtures/taxonomies/onboarding.md --seeds var/onboarding-review/seeds.json \
  --sample-kind synthetic --out var/onboarding-review/g2.json
```

阅读覆盖率、均衡性、边界与影响预览，认同时确认：

```bash
.venv/bin/papersteward onboarding confirm --gate g2 --review var/onboarding-review/g2.json \
  --by demo-user --summary '已审阅分类法与影响' --yes
.venv/bin/papersteward onboarding import-seeds
.venv/bin/papersteward onboarding submit calibration --file tests/fixtures/calibration/basic.json
.venv/bin/papersteward onboarding label agent --round 1 --file tests/fixtures/calibration/basic.json
.venv/bin/papersteward onboarding label user --round 1 --file tests/fixtures/calibration/basic.json \
  --out var/onboarding-review/g3.json
```

建轮只提取 cohort，不发送 fixture 中的答案。`label` 显式选择其角色批次；真实使用时
分别传入只含 source/labels 的 Agent 文件和 submitted_by/labels 的用户文件。
双方完成前 API 隐藏标注和参考答案，完成后查看完整差异与指标，再决定是否确认：

```bash
.venv/bin/papersteward onboarding confirm --gate g3 --review var/onboarding-review/g3.json \
  --by demo-user --summary '已审阅合成校准报告，仅流程验证' --yes
.venv/bin/papersteward onboarding submit dry-run --file tests/fixtures/dry_run/basic.json \
  --out var/onboarding-review/digest.json
```

如 G3 指标未达标且决定接受已知偏差，需额外提供
`--accept-deviations --known-biases '具体偏差及接受原因'`。这不能豁免结构/缺标/版本问题。

阅读日报、时间/配额/占比/权重/渠道/隐私后，可反馈问题：

```bash
.venv/bin/papersteward onboarding feedback --review var/onboarding-review/digest.json \
  --by demo-user --issue too_many --comment '数量超过当前阅读容量' \
  --out var/onboarding-review/feedback.json
```

可用问题为 `too_many`（太多）、`too_few`（太少）、`inaccurate`（不准）、
`wrong_category`（分类不对），`--issue` 可重复。存在问题时 G4 不会通过。
调整日报输入、将 version 从 1 改为 2 后重新 submit/review/feedback；也可对同版重新
审阅后明确认可，旧反馈会留在历史中。下面仅演示后一种路径：

```bash
.venv/bin/papersteward onboarding feedback --review var/onboarding-review/feedback.json \
  --by demo-user --accept --out var/onboarding-review/g4.json
.venv/bin/papersteward onboarding confirm --gate g4 --review var/onboarding-review/g4.json \
  --by demo-user --summary '已审阅并认可运行参数和试运行日报' --yes
.venv/bin/papersteward onboarding status
```

最终返回 active/revision=5。当前尚无真实发送器或调度器，不会因上述确认发送论文。

## 编辑、恢复与错误

```bash
.venv/bin/papersteward onboarding submit profile --id demo-profile --base-version 1.0 \
  --file tests/fixtures/profiles/v2.md --out var/onboarding-review/edited.json
.venv/bin/papersteward onboarding review profile --id demo-profile --version 1.1 \
  --base-version 1.0 --out var/onboarding-review/compared.json
.venv/bin/papersteward onboarding review taxonomy --id onboarding-taxonomy --version 1.0
.venv/bin/papersteward onboarding review calibration --round 1
.venv/bin/papersteward onboarding review dry-run --version 1 --out var/onboarding-review/recovered.json
```

档案编辑只在 profile 阶段允许，分类法编辑只在 taxonomy 阶段允许；以上编辑命令应在
相应确认之前使用。没有重置或再初始化入口，`start` 不撤销已通过的门。

- 返回 0 为成功，1 为输入/连接/服务端失败，2 为命令语法错误，Ctrl-C 返回 130。
- 连接失败：检查服务器 `alembic upgrade head`、Uvicorn 和 `--url`；CLI 不代为迁移。
- HTTP 400/422：按字段提示修正输入，保留原版本；提交新文档要提升版本。
- HTTP 409：状态/依据变化或版本冲突。先 status/review，用新文件保存、重新审阅；
  不自动刷新 hash/revision，也不自动重试变更。
- 输出文件已存在：请求前拒绝，换新文件名。
- 服务端操作已完成、但本地保存失败：用 review 恢复快照；不要重复 POST 创建同版。
- 撤销真实初始化需停止应用并恢复操作前数据库备份；仅删除快照/回退 CLI 不撤销门禁。

可自动复现的安全演示（只使用临时库）：

```bash
.venv/bin/pytest tests/e2e/test_onboarding_cli_e2e.py -m e2e -q
```
