# 首份日报测试步骤（个人优先）

> 目标：在**单用户、本地、低维护**的前提下，从空库走到第一份可审阅的日报，并判断
> 这套系统是否值得继续投入。不是产品发布、不是多用户验收，也不要求真实 SMTP。
> 建议总耗时：合成冒烟 30–45 分钟；真实首份日报 1.5–2 小时（取决于 LLM 和来源）。
> 相关契约：[P2 onboarding](phases/P2-onboarding.md)、[P6 delivery](phases/P6-delivery.md)、
> [P7 shelves](phases/P7-shelves.md)、[初始化 CLI 指南](onboarding-cli.md)。

## 0. 两种模式，先选一种

| 模式 | 用途 | 真实网络/LLM | 产出 |
|---|---|---|---|
| A 合成冒烟 | 验证安装、G1–G4、CLI、日报链路 | 不需要 | 合成日报，只看流程 |
| B 真实首份日报 | 验证内容质量、排序和阅读体验 | 需要真实来源；处理需要真实 LLM | 你自己的首份日报 |

**先做 A，再做 B。** A 失败说明环境/流程问题；B 失败才是产品问题。不要用 A 的
合成结果评价排序质量。

## 1. 准备隔离环境

不要直接拿已有数据库做第一次测试。用独立 SQLite 文件，失败可整体丢弃。

```bash
cd /path/to/PaperSteward
mkdir -p var/first-digest
export PAPERSTEWARD_DATABASE_URL='sqlite+pysqlite:////dev/shm/papersteward-first-digest.db'
export PAPERSTEWARD_TIMEZONE='Asia/Shanghai'
rm -f /dev/shm/papersteward-first-digest.db
.venv/bin/alembic upgrade head
```

终端 1 启动本地 API（首次测试不要绑定公网）：

```bash
export PAPERSTEWARD_DATABASE_URL='sqlite+pysqlite:////dev/shm/papersteward-first-digest.db'
export PAPERSTEWARD_TIMEZONE='Asia/Shanghai'
.venv/bin/uvicorn papersteward.main:app --host 127.0.0.1 --port 8810
```

终端 2 也导入同样的 `PAPERSTEWARD_DATABASE_URL`；CLI 的 onboarding 通过
`--url http://127.0.0.1:8810` 访问服务器：

```bash
export PAPERSTEWARD_DATABASE_URL='sqlite+pysqlite:////dev/shm/papersteward-first-digest.db'
export PAPERSTEWARD_TIMEZONE='Asia/Shanghai'
URL='http://127.0.0.1:8810'
.venv/bin/papersteward onboarding --url "$URL" status
```

预期：`status` 显示未开始/当前 gate；首次测试不要复用已有的 `var/papersteward.db`。

---

# A. 合成冒烟（30–45 分钟）

这一节只验证“系统能跑通”；G1–G4 和论文都使用 fixture，结论不能代表真实研究质量。

## A1. G1：提交并确认 profile

```bash
.venv/bin/papersteward onboarding --url "$URL" start
.venv/bin/papersteward onboarding --url "$URL" submit profile \
  --id first-profile \
  --file tests/fixtures/profiles/basic.md \
  --out var/first-digest/g1.json
```

阅读终端输出的全文、缺项和协商问题；确认这是“合成演示”，再执行：

```bash
.venv/bin/papersteward onboarding --url "$URL" confirm \
  --gate g1 --review var/first-digest/g1.json \
  --by local-user --summary '已审阅合成 profile，仅验证流程' --yes
```

## A2. G2：分类法与种子

生成 20 条合成种子（仅用于 taxonomy 覆盖率/均衡性检查）：

```bash
.venv/bin/python - <<'PY'
import json
from pathlib import Path
seeds = [
    {"paper_id": f"syn-T{tag:02}-{i}", "title": f"合成 {tag}/{i}", "primary_tag": f"T{tag:02}"}
    for tag in range(1, 6) for i in range(1, 5)
]
Path("var/first-digest/seeds.json").write_text(
    json.dumps(seeds, ensure_ascii=False), encoding="utf-8"
)
PY

.venv/bin/papersteward onboarding --url "$URL" submit taxonomy \
  --id first-taxonomy \
  --file tests/fixtures/taxonomies/onboarding.md \
  --seeds var/first-digest/seeds.json \
  --sample-kind synthetic \
  --out var/first-digest/g2.json
```

阅读覆盖率、均衡性、边界和影响预览，确认：

```bash
.venv/bin/papersteward onboarding --url "$URL" confirm \
  --gate g2 --review var/first-digest/g2.json \
  --by local-user --summary '已审阅合成分类法与影响' --yes

.venv/bin/papersteward onboarding --url "$URL" import-seeds
```

## A3. G3：合成校准

```bash
.venv/bin/papersteward onboarding --url "$URL" submit calibration \
  --file tests/fixtures/calibration/basic.json
.venv/bin/papersteward onboarding --url "$URL" label agent \
  --round 1 --file tests/fixtures/calibration/basic.json
.venv/bin/papersteward onboarding --url "$URL" label user \
  --round 1 --file tests/fixtures/calibration/basic.json \
  --out var/first-digest/g3.json
```

阅读指标；若指标未达标但接受偏差，使用
`--accept-deviations --known-biases '合成数据，仅流程验证'`。随后确认：

```bash
.venv/bin/papersteward onboarding --url "$URL" confirm \
  --gate g3 --review var/first-digest/g3.json \
  --by local-user --summary '合成校准，仅流程验证' --yes
```

## A4. G4：试运行与运行参数

```bash
.venv/bin/papersteward onboarding --url "$URL" submit dry-run \
  --file tests/fixtures/dry_run/basic.json \
  --out var/first-digest/g4.json
```

阅读日报、时间/配额/占比/权重/渠道/隐私，然后认可并确认：

```bash
.venv/bin/papersteward onboarding --url "$URL" feedback \
  --review var/first-digest/g4.json --by local-user --accept \
  --out var/first-digest/g4-accepted.json
.venv/bin/papersteward onboarding --url "$URL" confirm \
  --gate g4 --review var/first-digest/g4-accepted.json \
  --by local-user --summary '已审阅并认可合成试运行' --yes
.venv/bin/papersteward onboarding --url "$URL" status
```

预期 `ACTIVE`。G4 只证明门禁和参数确认，不证明真实内容质量。

## A5. 合成采集与处理（可选，仅链路）

```bash
.venv/bin/papersteward ingest --source fake \
  --fixture tests/fixtures/sources/p7.json \
  --query retrieval --run-id first-synthetic --json
```

从输出记录 5 个 paper_id，按标题分别用以下 fixture 处理（示例）：

```bash
.venv/bin/papersteward process --paper <topic-high-r-id> \
  --llm-fixture tests/fixtures/process/p7_topic_a.json --json
.venv/bin/papersteward process --paper <topic-high-e-id> \
  --llm-fixture tests/fixtures/process/p7_topic_b.json --json
.venv/bin/papersteward process --paper <special-1-id> \
  --llm-fixture tests/fixtures/process/p7_special.json --json
# special-2、special-3 同理
```

生成日报：

```bash
.venv/bin/papersteward scores snapshot --date "$(date +%F)" --json
.venv/bin/papersteward digest --date "$(date +%F)" \
  --out "var/first-digest/synthetic-$(date +%F).md" --json
```

打开 `var/first-digest/synthetic-*.md` 和 `http://127.0.0.1:8810/ui/today`，确认：
日报能生成、标题正确、特殊桶文献不在主队列、页面能打开。**不要评价内容好坏**。

---

# B. 真实首份日报（建议）

## B0. 换一个干净数据库

如果刚做完 A，**不要直接在同一个库上做 B**：A 的合成论文会混进 B 的采集、处理和
队列。停掉 Uvicorn，换一个新文件并重新启动；G1–G4 在 B2/B3 再用真实资料或
合成 fixture 完成：

```bash
export PAPERSTEWARD_DATABASE_URL='sqlite+pysqlite:////dev/shm/papersteward-first-real.db'
rm -f /dev/shm/papersteward-first-real.db
.venv/bin/alembic upgrade head
# 用同一个 DATABASE_URL 重新启动 uvicorn
```

如果跳过 A、直接做 B，也建议从空库开始；不要用已有真实数据做第一次测试。

## B1. 先决定是否使用真实 LLM

真实论文要经过 `process` 才可能进入队列；`process` 需要 relevance、summary、
classification 和特殊桶提名。因此：

- **没有真实 LLM**：只能做模式 A，或使用 fixture 论文；不能把真实论文处理成首份日报。
- **有真实 LLM**：配置 provider，显式 `--live`，并设置成本单价。

`.env` 示例（不要提交密钥）：

```dotenv
PAPERSTEWARD_LLM_PROVIDER=openai_compatible
PAPERSTEWARD_LLM_BASE_URL=https://api.example.com/v1
PAPERSTEWARD_LLM_MODEL=<model>
PAPERSTEWARD_LLM_API_KEY=<key>
PAPERSTEWARD_LLM_PROMPT_COST_PER_1K_USD=...
PAPERSTEWARD_LLM_COMPLETION_COST_PER_1K_USD=...
```

先检查：

```bash
.venv/bin/papersteward llm ping --live
```

不配置/不想联网时不要加 `--live`；不要用真实论文反复重试消耗预算。

## B2. 准备最小真实资料

复制 fixture 作为骨架，避免从零写 Markdown：

```bash
cp tests/fixtures/profiles/basic.md var/first-digest/profile.md
cp tests/fixtures/taxonomies/onboarding.md var/first-digest/taxonomy.md
```

编辑 `profile.md`：

- `profile_id` 改成自己的；
- `positioning`、`core_questions`、`subdirections`、`keywords`、`include/exclude` 改成真实方向；
- **保留 4 个 subdirections 且 `target_share` 合计 1**；
- `reading.weekly_capacity` 设成真实阅读量（例如 5 或 10）。

编辑 `taxonomy.md`：

- **保留 5 个主标签 T01–T05**（当前 G2 需要 5–8 个主标签；先用 5 个宽标签）；
- 把定义、纳入/排除、正例/边界改成自己的领域；
- 保留 fixture 的合成正例 ID 和边界条目，先让 G2 的结构校验通过；
- 如果想要真实边界例子，后续再替换种子并重新走 G2。

G1/G2 仍按 A1/A2 的步骤提交，只是文件换成 `var/first-digest/profile.md` 和
`var/first-digest/taxonomy.md`，`--id` 换成自己的 ID。**先编辑再提交，避免多次改版。**

## B3. G3/G4：先过门禁，不冒充真实校准

首份日报测试可以用合成 G3/G4 快速过门禁，但要在记录里注明“合成校准/试运行”：

- G3 使用 `tests/fixtures/calibration/basic.json`；
- G4 使用 `tests/fixtures/dry_run/basic.json`；
- 如果真实 profile/taxonomy 的 T 码/参数和 fixture 不兼容，先使用 fixture 资料完成
  A 模式；真实资料的质量评估留到第二次初始化或 P8。

**不要**把合成 G3 的 Spearman/Top-5 当作真实质量结论。

## B4. 采集 3–5 篇真实论文

有网络时用 arXiv（不需要 API key）：

```bash
.venv/bin/papersteward ingest --source arxiv --live \
  --query "retrieval augmented generation" \
  --max-results 5 \
  --run-id first-real-digest --json
```

没有网络时不要伪造真实论文；回到模式 A。记录：

- 这次 query 是否覆盖你的核心问题；
- 返回的 5 篇标题/摘要是否值得阅读；
- 有没有明显的重复/无关/太旧文献。

## B5. 处理并审核

```bash
.venv/bin/papersteward process --limit 5 --live --json
```

如果输出显示 `needs_review`，先处理审核，否则这些论文不会进入主队列：

```bash
.venv/bin/papersteward review list --json
.venv/bin/papersteward review approve <paper_id> \
  --by local-user --note '首份日报测试：确认分类' --json
# 分类不对时用 relabel，而不是强行 approve：
.venv/bin/papersteward review relabel <paper_id> --primary T0x \
  --by local-user --note '首份日报测试：改主标签' --json
```

处理完检查：

```bash
.venv/bin/papersteward queue --date "$(date +%F)" --json
```

至少要有 1 篇合格候选；如果为 0，先看 review/摘要/相关度门槛，不要反复生成日报。

## B6. 生成并阅读日报

```bash
.venv/bin/papersteward scores snapshot --date "$(date +%F)" --json
.venv/bin/papersteward digest --date "$(date +%F)" \
  --out "var/first-digest/real-$(date +%F).md" --json
```

打开文件；也可以打开 `http://127.0.0.1:8810/ui/today`。**第一次不要发 SMTP**，先看
文件；SMTP 需要 G4 明确选择渠道，不能临时绕过。

## B7. 首份日报评估表

逐篇填写，不要只看“有没有生成”：

| 字段 | 记录 |
|---|---|
| 标题/来源/链接 | 是否真实、可打开 |
| 相关度 | 与你的核心问题是否相关（高/中/低） |
| 摘要 | 是否忠实、100 字内、无编造 |
| 分类 | 主标签是否正确；是否需要 relabel |
| 特殊桶 | 是否应进综述/远邻/经典；档位/理由是否合理 |
| 阅读模式 | deep/skim 是否合理 |
| 最终动作 | 会读/跳过/延期；一句理由 |

整份日报检查：

- [ ] 最多 7 篇，单主标签最多 2 篇，deep 最多 2 篇、skim 最多 5 篇；
- [ ] 标题、摘要、理由、链接都能对上原文；
- [ ] 没有特殊桶文献混入主队列；
- [ ] 没有待审提名/未审核文献被当成合格候选；
- [ ] 没有把备注、密钥或完整论文正文发到外部；
- [ ] 你能在 10 分钟内决定“今天读哪 2 篇”。

## B8. 记录反馈（可选但推荐）

对确实读完的论文执行一次 `done`，确认阅读反馈和归档：

```bash
.venv/bin/papersteward done <paper_id> --mode deep --score 4 \
  --note '首份日报：方法可复现，值得跟进' \
  --request-id first-digest-<paper_id> --json
```

第二天再看 `queue`/`digest` 是否因反馈变化；首份测试不需要立刻调权重。

## 2. 通过/不通过标准

**流程通过**（可以继续）：

- G1–G4 全部显式确认，无绕门禁；
- 至少 1 篇真实论文完成 process 并进入队列；
- 日报生成、页面打开、文件可读；
- 没有崩溃、密钥泄漏或特殊桶混入主队列。

**内容暂不通过**（正常，记录问题后调整）：

- 5 篇里少于 2 篇值得读；
- 摘要/分类错误多；
- 主队列被综述/远邻/经典占据；
- 阅读量明显超过你的真实容量。

首份测试的目的是发现这些问题，不是证明系统已经“好用”。

## 3. 常见失败与处理

| 现象 | 处理 |
|---|---|
| `onboarding status` 连不上 | 服务器未启动/端口不对；用 `--url` 指向同一端口 |
| G4 失败 | 先读 review 中的具体缺项/反馈；不要跳过确认 |
| process 后 queue 为空 | 看 `review list`、摘要校验、R 门槛、特殊桶待审排除 |
| 日报没有某篇 | 该篇可能未过 R/摘要/分类门禁，或特殊桶/待审提名被排除 |
| 真实 LLM 报错 | 先 `llm ping --live`；检查 base URL/模型/密钥/预算；不要重试失控 |
| 想重来 | 使用新的 `/dev/shm` 数据库和新的 `var/first-digest-*` 目录；不要删除真实库 |

## 4. P8 边界：按“个人工具”而不是“产品”来做

你不需要现在写完整 PRD。这个项目的定位是满足自己的需要，顺便开源；P8 可以只做
**个人 pilot**，把产品化问题全部推迟：

### 现在只回答 5 个问题

1. 未来两周我是否每天愿意打开日报？
2. 日报里“值得读”的比例是否 ≥ 50%？
3. 无关推送/错误分类是否多到让我不再信任？
4. 每天/每篇的 LLM 成本是否可接受？
5. 出现一次故障/崩溃后，我能不能在 10 分钟内恢复？

### P8 只做 5 张卡（建议）

- **P8-S1 评估**：固定 20–30 篇离线样本 + 日报 golden；先做小，不追求学术评测。
- **P8-S2 可观测**：运行成功率、耗时、成本、队列深度；本地日志即可，不做遥测。
- **P8-S3 成本/降级**：每篇/每日预算、超预算降级为 skim/跳过、provider 失败不阻塞日报。
- **P8-S4 备份/恢复**：SQLite + 归档导出 + restore drill；不做高可用集群。
- **P8-S5 pilot 清单**：2 周使用记录、已知问题、README/runbook；不做 SLA。

### 明确不做（至少现在不做）

- 多用户/登录/权限/团队共享；
- SaaS/计费/公开注册/移动端；
- 全量 Telemetry、复杂 BI、A/B 实验平台；
- 真实 SMTP 的高可用投递、退信管理、多收件人；
- 为了“像产品”而增加的服务化/队列/微服务。

开源时只需保证：README 能跑、配置有样例、数据/密钥不外发、已知限制写清楚。
这些比“产品化完整度”重要。

## 5. 一页执行清单

- [ ] 独立数据库 + 本地 Uvicorn 启动
- [ ] A 模式 G1–G4 合成确认
- [ ] A 模式合成日报可打开
- [ ] 决定是否配置真实 LLM（无则不做 B）
- [ ] 编辑真实 profile/taxonomy（保留 5 个 T 码）
- [ ] G1/G2 用真实资料重新确认（必要时新库）
- [ ] arXiv 采集 3–5 篇 / process `--live`
- [ ] 处理 review，queue 至少 1 篇
- [ ] 生成 `real-YYYY-MM-DD.md` 并逐篇评估
- [ ] 记录“流程通过/内容暂不通过”的结论
- [ ] 把发现写给下一次迭代，而不是立刻加功能

> 首份日报的目标不是“发出去”，而是回答：**这套系统是否值得我明天再打开一次。**

## 6. 2026-10-23 首份日报测试记录

> 结论：**流程通过，内容召回暂不通过。** 用户接受本次日报。

### 环境与范围

- 独立测试库 `var/first-digest/night.db`，API `127.0.0.1:8811`；原
  `/dev/shm/papersteward-first-digest.db` 与 8810 服务未改动。
- G4 测试参数：`quiet_start=null`、`quiet_end=null`、`push_time=00:00`、
  `local_markdown`。仅用于夜间测试，不代表正式配置。
- LLM：`deepseek-chat`（`.env` 原为 `deepseek-flash`，在 JSON schema 字段类型
  与摘要长度上不稳定）。
- G1–G4 全部显式确认，最终状态 `active`。

### 结果

| 项目 | 数值 |
|---|---|
| 采集查询 | `long-horizon manipulation`、`embodied task planning`、`robot failure detection`、`LLM task planning` |
| 采集候选 | 16 |
| process 完成 | 16 |
| 进入队列 | 1 |
| 日报条目 | 1（GAVEL，T01，R=71.2，P=64.6，精读） |
| 待审 review | 13 |
| 特殊桶待审提名 | 16 |

- 日报：`var/first-digest/real-2026-10-23.md`；`/ui/today` 可打开，arXiv 链接返回 200。
- GAVEL 的摘要、主标签与证据一致；用户接受本次日报。

### 通过 / 未通过

- **流程通过**：G1–G4 显式确认；至少 1 篇真实论文完成 process 并进入队列；日报与
  UI 可读；特殊桶未混入主队列。
- **内容召回未通过**：16 篇采集只有 1 篇入队；多数 R 在 25–51；CycleVLA 相关度
  55.4，但摘要含未支撑数字被 `summary_not_passed` 挡住；GraphThink R=49.4 低于
  55 门槛。
- 主要原因：`hashing-v1` 语义相似度未校准；查询词偏 VLA/操作；摘要与分类门禁较严。

### 发现的问题

1. **Prompt 未内联 JSON schema**：`relevance/v1`、`summary/v2` 等只写“符合某某
   JSON schema”，真实模型会输出嵌套结构（如 `problem: {score, evidence}`）或超长
   摘要，触发 `schema.relevance.problem` / `schema.summary.summary_100` 校验失败。
2. **模型选择**：`deepseek-flash` 在 schema 上不稳定；本次改用 `deepseek-chat`
   才跑通。
3. **摘要长度**：`summary_100` 的 100 个非空白字符限制只写在 prompt 正文里，
   模型常输出 110–130 字；需要把约束放进 schema 并加强 repair 提示。
4. **相关度校准**：`hashing-v1` 未校准，R 普遍偏低，在 55 门槛下召回不足。
5. **临时绕行**：本次用 `var/first-digest/process_with_schema.py` 在运行时补
   schema 并截断摘要，**不是产品代码**；修复后应删除。

### 后续

- 代码：prompt 新版本内联 schema、repair 提示带 schema 与长度约束；补回归测试
  （本次已在 `fix/llm-prompt-schema` 修复）。
- 配置：`.env` 使用 `deepseek-chat`。
- 评估：P8-S1 固定样本评估相关度与摘要，再决定 embedding 和门槛。
- Pilot：按 P8 五问跑两周；查询改为主题轮换或 profile 生成查询。
