# P2 初始化协商与确认门

> 状态：P2-S1–S3 完成（2026-10-21）；S4–S6 未开始。依据：大阶段开发计划 §3 P2、设计方案 §4、P1 handoff §10.7。

## 小阶段卡片

共同约定：不接真实来源/LLM，不实现 P3–P6 算法；无新增依赖；每卡 0.5–2 天。

| 卡片 | 目标、输入 → 输出 | 契约与测试 | 验收命令 | 范围外 |
|------|------------------|------------|----------|--------|
| S1 | P1 契约 → 初始化状态机、持久化、确认事件、统一门禁、start/status API | 0003 可逆迁移；顺序/重复/过期确认拒绝；G4 前入队/推送/归档拒绝 | `make check`、`make test-integration`、`make migrate-check` | 草案、门槛业务证据生成、确认 UI |
| S2 | S1 + profile fixture → 档案草案/导入/编辑/确认，接入 G1 | 复用 documents.service；确认绑定文档 ID/版本；非法档案不推进 | `make check`、profile/onboarding 集成测试 | 分类法/校准 |
| S3 | S2 + taxonomy/种子标注 → 分类法校验、确认、影响预览，接入 G2 | 5–8 主标签、覆盖率 ≥90%、单桶 ≤50%；边界失败用例 | `make check`、taxonomy/onboarding 集成测试 | 真实分类算法 |
| S4 | S3 + 合成种子 → 幂等导入、15–20 篇校准 harness，接入 G3 | Spearman/Top-5 golden、已知偏差书面接受；报告绑定前序版本 | `make check`、calibration 集成测试 | 真实来源/评分实现 |
| S5 | S4 + 固定参数/日报 fixture → 只展示试运行、参数确认，接入 G4 | 版本绑定、显式确认；真实 Uvicorn 门禁 e2e；不发送 | `make check`、`make e2e`、`make demo` | 正式推送/编排算法 |
| S6 | S1–S5 API → 最小 CLI/HTMX 流程与阶段验收 | 薄 UI；完整 G1–G4 路径；错误可操作 | 完整阶段门（含 secret-scan） | 高级交互、多用户 |

S2–S6 开工时补充实际文件与 fixture 清单；不得据本表提前实现。

## S3 执行卡（2026-10-21）

- 目标：分类法草案/追加编辑版本 → 完整校验与影响预览 → 显式确认文档并原子通过 G2。
  依赖 S2（40f65f7）；不推进 S4 种子导入和校准。
- 输入：P1 taxonomy/profile fixture、新增 `taxonomies/onboarding.md` 和测试内合成标注；
  用户/Agent 提交 Markdown 及完整种子清单（paper_id/title/primary_tag，未分类用 null）。
- 输出：onboarding/taxonomy.py、api/onboarding.py、事件枚举/确认服务小扩展、单元/
  集成测试、合成 fixture、ADR 0003 与状态/交接文档。无依赖/迁移，head 0003。
- API：POST `/onboarding/taxonomy/drafts`；GET `/onboarding/taxonomy/{id}/{version}`
  返回报告、全文 hash、review_hash、绑定档案、创建时 base_version 的 diff；POST 同路径
  `/confirm` 携带明确确认、用户/摘要、revision、content_hash、review_hash。
- 只在 taxonomy 阶段写草案/确认。精确绑定 G1 事件选定的 confirmed Profile；档案
  被归档、正文或缓存发生变化时拒绝继续。草案及证据同事务追加，不覆盖历史版本。
- 校验：G2 5–8 主标签（P1 parser 的 5–9 不变）；每标签 2–4 句定义、纳入/排除、
  ≥3 个不同且匹配种子标注的正例、≥2 个不同边界案例及合法判定/理由；覆盖率 ≥90%，
  最大单标签占比 ≤50%。空清单不能通过；重复 ID 拒绝，null 计入分母，未知代码报错。
  文档 UTF-8 ≤128 KiB，拒绝 YAML 锚点/别名；样本最多 200，性质 user/synthetic。
- 影响预览：逐标签数量/占比、未分类与问题、种子移动、边界变化、规则变更对查询/
  rubric 的待复核点、既有 queued/scored/snoozed 文献候选；仅展示，不重分、不改分。
- 错误/测试：沿用 400 字段错误、404、409 版本/状态冲突、422 请求错误；覆盖阈值
  正反例、缺失/重复/越界证据、错档案绑定、陈旧预览、缓存漂移和原子回滚。
- 验收：make check/test-integration/migrate-check、可复制 API 演示；secret-scan。
  回滚和持久化依据见 ADR 0003；超出文件/行数指引在本阶段 handoff 说明。

## S2 执行卡（2026-10-21）

- 目标：Agent/用户提交可解析的 Markdown 草案，用户审阅/编辑为新版本，再显式确认
  档案并原子通过 G1。依赖 S1（已提交为 71ae62c），复用 P1 文档服务、解析器与 diff。
- 输入：`tests/fixtures/profiles/basic.md`、`v2.md`、`invalid.md`；不生成虚构研究方向，
  不调用真实模型。草案来自 Agent/用户 Markdown，系统返回最多 5 个协商问题。
- 输出：`onboarding/profile.py`、扩展 `api/onboarding.py`；单元/集成测试；本交接、
  CHANGELOG、README、AGENTS。无依赖与 schema 变更，head 保持 0003。
- API：POST `/onboarding/profile/drafts`（content_md/profile_id/base_version）；
  GET `/onboarding/profile/{id}/{version}`（全文、hash、G1 缺项、问题；可选 base_version 比较）；
  POST `/onboarding/profile/{id}/{version}/confirm`（confirmed=true、用户、变更摘要、
  expected_revision、content_hash）。确认者由本地用户提供，不引入鉴权。
- 草案/编辑只允许 profile 阶段；编辑追加新版本，指定已存在的 base_version，展示
  结构化 diff 与 Markdown diff，不覆盖旧版本。版本字符串不推断大小，确认精确指定
  的版本；旧版本仍可被明确选中，不自动确认“最新”。
- G1：重新解析正文事实源，不信任 parsed_json 缓存；核心问题/子方向/纳入标准复用
  P1 校验，额外要求明确排除标准、阅读目标及子方向关键词。未完整草案可保存并协商，
  不可确认；不提前检查 G4 的推送参数。全文 UTF-8 ≤128 KiB，不接受 YAML 锚点/别名。
- 错误：语法/缺项 400（field/line）、不存在 404、错阶段/重复版本/过期 revision/hash
  冲突 409、请求结构 422；全部沿用统一错误信封。失败不改变档案状态/G1/事件。
- 测试：草案正反例、最多 5 个问题、版本 diff/旧版保留；完整 API 路径；拒绝不完整/
  陈旧/重复确认、未 start、G1 后编辑、超大文档、缓存不一致、审计失败原子回滚。
- 验收：`make check`、`make test-integration`、`make migrate-check`，可复制 API 演示。
  不在范围：G2、校准、试运行、初始化 CLI/页面（S6）、再初始化与真实模型。
- 回滚：无迁移；回退 S2 代码即可恢复 S1。新建档案版本保留，已通过 G1 的状态不
  自动撤回；需要完整回到 S2 前状态时停止应用并恢复操作前数据库备份。

## S1 冻结契约与范围

- `seeds → profile → taxonomy → calibration → dry_run → active`；种子可选，start
  直接进入 profile。无记录等同 seeds；GET 只读，start 幂等，ACTIVE 不可被 start 重置。
- G1/G2/G3/G4 分别允许 profile/taxonomy/calibration/dry_run 前进一步；禁止跳门、
  倒退、重复确认。每次确认携带当前 revision、用户标识、产物 ID/版本和变更摘要。
- S1 提供内部 `confirm_gate` 接口，**必须注入可信业务证据校验器**；外部请求不能
  上传 `passed=true` 或选择校验器。S2–S5 分别实现真实证据校验；S1 不提供通用
  HTTP 确认接口。测试中的成功路径使用明确标识的合成校验器，仅验证流程。
- 单用户 singleton 表 `onboarding` 与迁移 0003；revision 条件更新防止过期确认；
  状态和事件同事务写入。服务不 commit，由调用方 session_scope/显式事务提交。
- 追加 `onboarding.started`、`onboarding.g1.confirmed` 至 `onboarding.g4.confirmed`
  事件。事件保存确认者、产物 ID/版本、摘要与迁移两端状态/revision。
- `PaperRepository.add/transition` 的 queued/pushed/archived 入口与
  `ArchiveSnapshotRepository.add` 共用 ACTIVE 检查；无记录也拒绝。
  纯领域状态机仍只判断合法边，不隐式访问 DB；未来外部推送必须先调用同一门禁。
- API：`GET /onboarding` 返回 stage/revision/next_gate；`POST /onboarding/start`
  显式开始/幂等恢复。无种子导入、草案、校准、日报或推送端点。

## S1 验收与 handoff

### 能做什么

- 持久化/恢复初始化进度；开始幂等，不重复记事件；内部顺序确认记录用户和产物版本。
- 拒绝跳门、重复门、过期 revision、校验器拒绝；状态与事件同事务撤销。
- 所有现有仓储 queued/pushed/archived 写入入口受统一 G4 门禁约束，包括特殊桶
  shelved → pushed、稍后重入队及直接 add；原有文献状态机仍有效。
- start/status 可经 API 演示（先显式迁移并启动本机 API）：

```bash
.venv/bin/alembic upgrade head
.venv/bin/uvicorn papersteward.main:app --host 127.0.0.1 --port 8000
# 在另一个终端运行；第二次 start 返回相同进度
curl -fsS http://127.0.0.1:8000/onboarding
curl -fsS -X POST http://127.0.0.1:8000/onboarding/start
```

### S1 结束时的限制与 S2 输入（历史交接）

- 目前无外部通用确认端点、初始化 CLI/页面、业务校验器、种子导入、校准或日报。
  测试中的 ACTIVE 是合成流程证据，不代表真实业务初始化已验收。
- S2 可直接复用 `core.onboarding.GateApproval`、`onboarding.service.confirm_gate`
  和 `storage.onboarding.get_onboarding/ensure_active`。S2 的可信 G1 校验器应检查
  确认产物 ID/版本、已确认 Profile 与内容完整性，再由明确用户确认命令调用。
  状态推进、产物确认和事件应置于同一 session_scope 中。
- G2/G3/G4 校验器依次检查覆盖率/均衡/边界、校准指标或书面接受、参数及试运行认可；
  必须绑定前序文档版本。不得从外部 JSON 构造“已通过”并直接推进。
- 目前没有正式发送服务；P6 的发送操作必须先调用 ensure_active。
- 重大方向变化再初始化/撤销激活不在 S1；不得以 start 重置已确认状态。

### 验证证据

2026-10-21 本地 Python 3.12：

- `make check`：ruff format/check、strict mypy 通过；**380 passed**，1 e2e deselected。
- `make test-integration`：**114 passed**（包含 API、0003 往返与模型一致性）。
- `make migrate-check`：base → 0003 → base → 0003 成功。
- `make e2e`：真实 Uvicorn walking skeleton **1 passed**。
- `make demo DEMO_PORT=8775`：临时库迁移/seed/health/papers 成功。
- `make secret-scan`、`git diff --check`：通过。
- 新增 41 个测试实例（29 单元 + 12 集成）；覆盖所有门前状态、确认失败、审计失败、
  外层事务回滚、跨 Session 恢复、ACTIVE 后允许操作和原有迁移约束。
- 测试发现并修复 SQLite legacy SAVEPOINT 在外层未实际 BEGIN 时提前提交的问题；
  局部原子块显式确保外层事务存在，不改变全局连接配置。
- 运行环境普通文件系统上 SQLite 同步写入挂起（最小 CREATE TABLE 可复现），因此
  上述 DB 验收使用 `/dev/shm` 临时文件库，未降低 synchronous 设置，未触碰 var 库。
  可复现命令如下；Docker/CI 未在本轮重新验证，不声称 P2 阶段整体验收。

```bash
make check PYTEST='.venv/bin/pytest --basetemp=/dev/shm/papersteward-p2-s1-check'
make test-integration PYTEST='.venv/bin/pytest --basetemp=/dev/shm/papersteward-p2-s1-integration'
TMPDIR=/dev/shm make migrate-check
TMPDIR=/dev/shm make e2e PYTEST='.venv/bin/pytest --basetemp=/dev/shm/papersteward-p2-s1-e2e'
```

### 风险、回滚与规模说明

- 迁移仅新增 onboarding；不回填旧库为 ACTIVE，不改变既有领域表内容。
- 停止应用后 `alembic downgrade 0002` 删除初始化状态；审计事件保留。重新 upgrade
  后门禁关闭。完整回滚旧版本代码须恢复 P2 前备份（旧枚举不认识新事件）。
- 本次超过 ≤8 文件/≤400 行指引：状态/迁移/仓储执行门禁/API 构成一个完整竖切，
  加上失败与回滚测试、P1 迁移 head 断言更新、ADR/阶段卡片/交接与状态文档。
  无新增依赖，未推进 S2–S6；P1 既有表/schema 不变，freeze 测试保留旧契约并允许
  迁移追加新表。
- P1 F5（Starlette/httpx 弃用警告）仍为非阻断依赖债务。

## S2 验收与 S3 handoff

### 现在能做什么

- 通过三个 profile API 提交/编辑/审阅 Markdown 草案，再显式通过 G1。结构不合法的
  草案返回带字段/行号的错误；结构合法但排除标准、阅读目标或关键词缺失时，允许
  保存协商并返回缺项，不能确认。协商问题最多 5 个。
- 编辑要求指定已有 base_version 并使用新版本号；旧版不可覆盖。响应同时提供
  结构化 diff 和 Markdown diff，正文变更可审阅；GET 加 base_version 查询参数可
  重现版本差异。版本字符串没有“大小”推断，
  用户明确选择哪个版本就审阅/确认哪个版本。
- content_hash 基于审阅响应中的持久化 Markdown（复用 P1 首尾空白规范化），
  确认时应原样回传，不要对原始文件自行计算。确认同时校验 revision，防止陈旧提交。
- 确认前重新解析 Markdown，检查缓存与 ID/版本一致性；档案确认/旧确认版归档/
  文档事件/G1 事件/流程推进置于 S1 原子块中，任一步失败全部回滚。
- P1 已确认且完整的文档可复用，不重复写 profile.confirmed；已归档文档不可通过 G1。
- 不开放文件路径读取接口，不接真实 LLM。Agent 草案通过 Markdown API 提交；
  研究方向的语义内容由用户/Agent 提供，系统不凭空推测研究方向。

### 验收证据

2026-10-21 本地 Python 3.12，DB 测试延续 S1 的 `/dev/shm` 临时文件库隔离方式：

- `make check`：ruff format/check、strict mypy 通过；**399 passed**，1 e2e deselected。
- `make test-integration`：**127 passed**。
- S2 新增 **19** 个测试实例（6 单元 + 13 集成），包含完整 API 路径、文档 ID/版本
  边界、缺项协商、过期 hash/revision、未明确确认、重复版本、缓存不一致、旧版归档、
  已归档拒绝、事件失败回滚与 UTF-8 输入限长。
- `make migrate-check`：upgrade/downgrade/upgrade 成功；模型一致性集成测试通过，
  无迁移，head 仍为 0003。
- `make e2e`：真实 Uvicorn walking skeleton **1 passed**。
- S2 真实 Uvicorn 演示：合成 basic.md → v2.md → 全文审阅 → G1 确认，返回
  `stage=taxonomy/revision=2/next_gate=g2`；重复确认返回 409；临时库随演示清理。
- `make demo DEMO_PORT=8775`：临时库 health/papers 演示通过。
- `make secret-scan`、`git diff --check`：通过；Docker/CI 本轮未重跑。

可复制的专项验收（使用合成 fixture，不触碰用户库）：

```bash
.venv/bin/pytest tests/unit/test_onboarding_profile.py \
  tests/integration/test_api_onboarding_profile.py -q \
  --basetemp=/dev/shm/papersteward-p2-s2-demo
```

### S2 结束时的 S3 输入与边界（历史交接）

- 已就绪：G1 确认事件绑定 artifact_id/artifact_version，Profile 为 confirmed；S3
  应据该事件加载精确档案版本，不能随意取另一个文档 ID 的 latest_confirmed。
- 复用 `onboarding.service.confirm_gate` 接入 G2 可信校验器；分类法草案/编辑只允许
  taxonomy 阶段。S3 需覆盖 5–8 主标签、种子覆盖率/均衡、边界例子与影响预览。
- S2 的 G1 确认已固定为专门端点，外部请求不能指定任意 gate 或业务校验器。
- 当前 G2–G4 保持未实现；没有初始化 CLI/页面、种子导入、校准/试运行、真实调用。
  G1 后仍禁止正式入队/推送/归档；不要在 S3 中提前实现 S4/S5。
- 重大方向变更与撤销初始化仍不在 S2；P1 通用文档命令不自动推进 G1。后续更新
  已选定档案时，必须按再协商契约处理，不能把新文档版本视为当前 G1 已确认版本。

### 风险、回滚与规模

- 无新增依赖、迁移、P1 schema 修改。回退 S2 代码可恢复 S1 API；新增文档和确认
  状态仍保留，若要撤销整个演示/初始化需停止应用并恢复操作前备份。
- 修改 8 个文件；净变更超过 400 行指引，主要为 service/API 竖切、完整失败/原子性
  测试及执行卡/交接文档。未修改 P1 通用解析/确认命令语义，未推进 S3。
- P1 F5（Starlette/httpx 弃用警告）仍为非阻断依赖债务。


## S3 验收与 S4 handoff

### 现在能做什么

- taxonomy 阶段可提交/编辑分类法 Markdown 和完整种子标注，保存为新版本。
  GET 会恢复创建时的基线与种子证据，并返回全文、结构化/Markdown diff、content_hash、
  review_hash 和完整报告。不推断版本大小，不覆盖旧版本。
- G2 校验 5–8 个启用主标签、≥90% 覆盖率、≤50% 单标签占比；每标签定义 2–4 句、
  纳入/排除、3 个不同且匹配种子标注的正例、2 个不同边界案例及合法判定/理由。
  null 标注仍计入分母；重复 paper_id 拒绝；未知主标签会报错。未完整草案可保存、
  可查看缺项，但无法确认。P1 通用解析器的 5–9 标签上限保持不变。
- 边界行格式为 `- 边界：案例；判为：Txx；理由：说明`，每个主标签至少两行。
  定义句界机械按句号/问号/叹号统计；语义互斥和标注质量仍由用户审阅，不声称已经
  完成独立分类评估。新合成 fixture 的指标仅证明流程，不代表真实研究方向质量。
- 报告展示逐标签数量/占比/文献 ID、未分类项、种子移动/移除、边界判定变化、
  规则对查询与 rubric 的待复核点和既有未读队列影响。仅改边界规则也会提示复核。
  未导入 Paper、改写分类/桶/评分或执行真实查询。
- P1 PaperTag 没有 taxonomy_id，因此既有队列影响是按版本/代码匹配的候选，报告
  明示待人工复核；无法确定目标标签的项目返回 null。没有静默执行批量重分类。
- 追加 onboarding.taxonomy.drafted 事件保存全文 hash、G1 事件/档案精确版本/hash、
  全部种子标注、sample_kind 和 base_version。编辑可更换种子清单，但会显示基线
  样本数与 removed_seed_ids，不隐藏样本移除。
- G2 只允许 confirmed 的 G1 精确档案；归档/缓存漂移或草案绑定 hash 变化会拒绝。
  确认前重新解析正文、核对标签缓存、重算报告，并验证用户回传的双 hash 与 revision。
  文档确认、旧确认版归档、G2 状态/事件同事务提交，失败全部撤销。
- G2 事件 evidence 保存 draft_event_id、绑定 profile、review_hash、sample_kind 和
  完整 report 快照。队列以后改变也能核对当时确认过的报告；草案种子依据仍可按事件
  引用读取。只确认 G2，不开放任意 gate/客户端校验器。

### 验证证据

2026-10-21，本地 Python 3.12；延续 S1 的 /dev/shm 临时文件库隔离方案：

- 最终 `make check`：ruff format/check、strict mypy 通过，**427 passed**，1 e2e deselected。
- `make test-integration`：**141 passed**；最终 make check 也包含全部集成测试与快照断言。
- 新增 **28** 个测试实例（14 单元 + 14 集成）：90%/50% 等号边界、空/未知/重复种子、
  9 标签/不足正例/缺失或重复边界、不完整草案拒绝、版本编辑与只读影响、精确 G1
  绑定/档案漂移、陈旧 hash/revision、标签缓存漂移、审计失败回滚、边界单独变更和
  种子移除可见性。
- `make migrate-check`：base → 0003 → base → 0003；模型一致性集成测试通过，
  无新增依赖/迁移，未修改 P1 表字段或文档通用解析/确认语义。
- `make e2e`：真实 Uvicorn walking skeleton **1 passed**。
- S3 真实 API 演示：G1 → 合成分类法（5 标签、20 种子、覆盖 100%、最大桶 20%）
  → 审阅 → G2 确认，返回 calibration/revision=3/next_gate=g3；重复确认 HTTP 409。
  临时库随演示清理，无用户库写入或真实模型调用。
- `make demo DEMO_PORT=8775`：临时库迁移/seed/health/papers 成功。
- `make secret-scan`、`git diff --check`：通过。Docker/CI 本轮未重跑，不声称 P2 整体验收。

可复制的专项验收：

```bash
.venv/bin/pytest tests/unit/test_onboarding_taxonomy.py \
  tests/integration/test_api_onboarding_taxonomy.py -q \
  --basetemp=/dev/shm/papersteward-p2-s3-demo
```

### S4 直接输入、限制与回滚

- 读取 onboarding.g2.confirmed 的 artifact_id/artifact_version，加载精确 confirmed
  Taxonomy；读取 evidence.profile 校验已选档案，不能随意取另一个 ID 的 latest。
- 通过 evidence.draft_event_id 读取版本化种子依据（seeds、sample_kind、base_version），
  evidence.report 为 G2 审阅快照。S4 应在当前 calibration 阶段实现幂等 Paper 导入、
  独立 Agent/用户标注与 15–20 篇校准指标，再用可信校验器接入 G3。
- 当前种子依据只有 ID、标题和主标签，不含独立相关度评分/阅读建议，不能直接当作
  “校准通过”。S4 仍需相应 fixture 和已知偏差书面接受流程。
- G3/G4、初始化 CLI/页面、真实来源/模型、运行参数与目标占比确认尚未实现；
  G2 后仍禁止入队/推送/归档。重大方向变化和撤销初始化不在 S3。
- 设计取舍见 ADR 0003：边界规则从正文读取，未新增独立边界库/Tag 字段。若未来需要
  独立编辑/检索的边界案例实体，应新增迁移，而不是改变冻结 P1 契约。
- 无 schema 回滚；代码回退不删除文档/事件，旧枚举不能读取新增事件，完整恢复 S2
  须停止应用并恢复 S3 前数据库备份。已过 G2 的状态不自动撤销。
- 本次修改 12 个文件、超过 400 行指引：包含完整 G2 校验/影响预览/审计竖切、28 个
  失败与回滚测试、合成 fixture、必要 ADR/交接/能力说明。无无关重构，未推进 S4。
- P1 F5（Starlette/httpx 弃用警告）继续保留为非阻断依赖债务。
