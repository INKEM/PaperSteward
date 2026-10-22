# P2 初始化协商与确认门

> 状态：P2-S1–S6 实现与本地阶段验收完成（2026-10-22）；Docker 与 CI 已独立验证，P2 验收通过；阶段 tag 待发布流程。独立报告见 [P2-independent-acceptance.md](P2-independent-acceptance.md)。依据：大阶段开发计划 §3 P2、设计方案 §4、P1 handoff §10.7。

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

## S6 执行卡（2026-10-22）

- 基线 S5 `72336df`；交付最小 onboarding CLI 与 P2 阶段验收。选择 CLI 优先的
  本地 HTTP 薄客户端，复用 S1–S5 API；无新增依赖/迁移，不实现 P3 算法或网页。
- 文件：cli/onboarding.py（参数/展示/审阅快照/确认）、cli/onboarding_client.py
  （标准库 HTTP 与本地快照）、cli/main.py 注册；unit/integration/e2e CLI 测试；
  ADR、操作指南、阶段验收/handoff、README/CHANGELOG/AGENTS。
- 命令覆盖 start/status、profile/taxonomy Markdown 提交、版本审阅/差异、种子导入、
  校准建轮和两方分别标注、参数/日报、四类反馈、G1–G4 显式确认。
- 审阅保存含服务器/阶段/revision/完整响应和校验和的本地 JSON（不覆盖已有文件）；
  confirm 仅用已审快照，要求 --gate/--by/--summary/--yes，不刷新依据或自动认可；
  G3 接受偏差必须显式选择并说明。API 继续负责所有可信门槛与事务校验。
- 连接只允许 loopback，禁用代理/重定向，设置超时；文件/连接/HTTP 失败有可操作
  提示及非零退出；不自动迁移、重试变更或输出配置/密钥。
- 验收：完整 make check/test-integration/migrate-check/e2e/demo/secret-scan；真实
  Uvicorn 的 CLI G1–G4 演示；恢复/过期快照/未确认/连接失败/无写入等回归测试。
- P2 结束写验收矩阵、能力/限制/回滚与可直接开始的 P3 handoff。Docker/CI 有证据
  才标记通过；本卡不提前开始 P3，不自动把测试认可用于用户数据。

## S5 执行卡（2026-10-21）

- 基线 S4 `5e843fc`；目标：固定 fixture 日报展示 → 参数/反馈协商 → 用户认可最新
  版本 → G4 原子确认；真实 Uvicorn 走通 G1–G4，验证入队/推送/归档门禁。
- 文件：新增 onboarding/dry_run.py（输入/参数/渲染）、dry_run_service.py（版本/
  反馈/G4）、合成 dry_run fixture 与 unit/integration/e2e 测试；扩展 onboarding API
  与事件枚举，更新 ADR/阶段 handoff/README/CHANGELOG/AGENTS。无依赖/迁移。
- 输入：运行参数（时间/时区、安静时段、暂停、目标主标签占比、渠道/隐私）、P1
  ScoringConfig（含配额/权重）、预选合成日报条目；只校验与展示，不排序/计算评分。
- 以 append-only Event 保存递增整数版本、完整输入与 G3/文档绑定、结构化报告和
  Markdown/hash；GET 重算验证。反馈独立追加，支持太多/太少/不准/分类不对；G4
  只接受最新日报和最新认可反馈，须回传双 hash、revision=4 与显式用户确认。
- G4 同事务写入选定 scoring（已有同版本须精确匹配）与 onboarding_runtime 配置、
  认可快照、审计和 active/revision=5；任何失败回滚。参数时间/隐私仅确认意图，不启动
  调度器或真实 provider；渠道限 local_markdown，备注禁止外发。
- API：POST /onboarding/dry-runs；GET /onboarding/dry-runs/{version}；POST 同路径
  /feedback、/confirm。没有虚构的发送接口；e2e 通过 HTTP 推进流程后调用真实仓储
  验证 G4 前拒绝 enqueue/push/archive，G4 后放行并回滚测试操作。
- 验收：make check/test-integration/migrate-check/e2e/demo/secret-scan；用 tmp_path
  和 /dev/shm 临时库。覆盖参数跨字段约束、转义/可复现日报、反馈拒绝/过期、G3/
  文档/配置漂移、旧版本选择、原子回滚与完整门禁链路。
- 范围外：真实来源/摘要/评分/发送/调度、特殊桶实现、CLI/UI（S6）、再初始化。

## S4 执行卡（2026-10-21）

- 目标：G2 精确种子依据 → 幂等导入；独立留出 cohort → 双方盲标 → 校准报告 → G3。
  依赖 S3（0fda9b6）；只做 S4，不实现日报、G4、真实采集/评分或初始化 UI/CLI。
- 新文件：onboarding/calibration.py（输入/纯指标）、calibration_service.py（事务与
  绑定）、0004 迁移、合成 calibration fixture、unit/integration 测试；扩展 models、
  事件枚举和 onboarding API，并更新迁移 head 测试与必要状态文档。无新增依赖。
- API：POST /onboarding/seeds/import；POST /onboarding/calibration/rounds；GET
  /onboarding/calibration/{round}；POST 同路径 /agent-labels、/user-labels、/confirm。
- seed 导入只读 G2 draft_event_id 的完整清单，重复导入幂等，ID 内容冲突整批回滚。
- cohort 15–20 篇，与 G2 种子分离；按 ADR 0004 检查分层、标签覆盖、边界/负例。
  首轮编号 1；首轮报告完整后才能建第 2 轮；禁止第 3 轮，旧轮不能覆盖或挑选确认。
- 输入：固定 cohort（ID/title/abstract、参考主标签、分层/边界标签/负例元数据）、
  Agent R 0–100/主标签/阅读方式/理由、用户相关度 0–3/愿读/主标签/阅读方式。
  角色提交各自不可变完整批次；双方完成前不回显对方标注或 cohort 参考答案。
- 报告：Spearman/Top-5、分类和阅读方式一致率、边界争议、方向/rubric 调整建议；
  常数向量、并列排序和输入乱序均有固定结果；不写评分/权重/阅读反馈表。
- G3：confirmed=true、用户/摘要、expected_revision=3、report_hash；未达标还需
  accept_deviations=true 与非空 known_biases。结构或绑定错误永不由接受偏差绕过。
- 测试：golden/并列/常数/反例、幂等和冲突导入、隐藏对方标注、错样本/重复提交、
  两轮上限、陈旧报告/版本漂移、书面接受、事务失败回滚、迁移升降及模型一致性。
- 验收：make check/test-integration/migrate-check、API 演示、secret-scan；风险/
  回滚见 ADR 0004。规模超指引时在 handoff 说明。

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


## S4 验收与 S5 handoff

### 能做什么与持久化契约

- 分支 `feat/p2-s4-calibration-harness`，基线 S3 `0fda9b6`；迁移 head `0004`。
  设计决策见 [ADR 0004](../adr/0004-calibration-rounds.md)，无新增依赖。
- `POST /onboarding/seeds/import` 无输入清单，只导入 G2 evidence.draft_event_id
  的种子。重复调用返回 created=0/existing=N，审计事件只追加一次。相同 ID/标题保留
  既有摘要、作者、状态；标题冲突整批回滚。只新增 discovered 元数据。
- `POST /onboarding/calibration/rounds` 接收 round_number=1|2、sample_kind=user|
  synthetic、rubric_version、papers；cohort 必须与 G2 种子 ID 分离。新导入文献与
  cohort 标题/摘要必须精确一致；缺摘要不能覆盖既有摘要。cohort 结构校验不能豁免。
  首轮报告完整后才允许第二轮；只允许确认最新轮，最多两轮，不覆盖历史。
- 表 onboarding_calibration 独立保存 cohort、G2 event FK、profile/taxonomy 内容
  哈希绑定、rubric 标识、双方批次和报告。两轮上限、sample_kind 与报告需双方标注
  由 CHECK/FK 约束；应用服务负责完整性/顺序与单方提交 CAS。
- Agent 输入 source + labels（paper_id、R relevance=0–100、primary_tag 或 null、
  read_mode、reason）；用户输入 submitted_by + labels（relevance=0–3 严格整数、
  willing_to_read 布尔、primary_tag 或 null、read_mode）。各方须恰好完整标注一次；
  不接收夹带另一角色字段。阅读方式使用 P1 ReadMode（deep/skim）。
- 双方完成前 GET 和提交响应只返回论文 ID/标题/摘要、提交标记与空报告，隐藏参考标签、
  抽样分层、负例标记、双方标注。双方完成后报告包含这些 cohort 依据和完整配对标注。
  无多用户鉴权；这保证接口展示独立性，不提供身份隔离，也不能验证操作者是否看过答案。
- Spearman 使用平均并列秩；任一方恒定时返回 null，不能自动通过。Top-5 按分数降序、
  paper_id 升序破并列；报告还含主标签/阅读方式一致率、边界争议列表与数量、复核建议。
  高 R 而不愿读/低相关优先建议修档案，较大分差建议查 rubric；这只是启发式诊断，
  不自动修改权重、档案或 rubric。rubric_version/source 是提交者提供的溯源标识，
  当前不解析 rubric 正文，不做真实 Agent 推理。
- `POST /onboarding/calibration/{round}/confirm` 必须显式 confirmed=true，携带
  confirmed_by/change_summary、expected_revision=3 和审阅返回的 report_hash。
  Spearman≥0.6 且 Top-5≥3 自动满足指标；否则必须 accept_deviations=true 且
  known_biases 非空。接受偏差只豁免指标，不能豁免结构/缺标/版本或报告漂移。
- G3 可信校验器重算并核对报告；同事务推进 dry_run/revision=4/next_gate=g4，
  追加 onboarding.g3.confirmed，evidence 包含完整报告/hash、已知偏差与接受标记。
  校准创建/双方提交/报告各有追加事件。任何审计失败均回滚相应标注/报告或门禁状态。
- G2 或 cohort 任一为 synthetic，报告都标记“仅流程验证”；不会用 metrics_passed
  声称真实模型能力。标注保存在独立校准表，可供后续偏好功能读取，不写 Rating、
  PaperTag、ScoreSnapshot、推送或归档表。

### 验收证据（2026-10-21）

- `make check`：ruff format/check、严格 mypy（63 源文件）、486 个非 e2e 测试通过。
- `make test-integration`：174 个通过；本轮新增 59 个测试（unit 26/integration 33），
  覆盖 golden/并列/常数/乱序、独立留出/结构失败、幂等/冲突导入、盲标/角色完整性、
  低指标书面接受、两轮/最新轮限制、陈旧报告/revision、档案/分类法/论文/报告漂移、
  标签/报告/门禁审计冲突、倒退时间、调用方捕获失败后提交与外层 rollback、DDL 约束。
- `make migrate-check`：base→0004→base→0004；模型一致性检查通过；有数据的
  0004→0003→0004 保留 Paper 与审计事件，校准表被删除后重新创建为空。
- `make e2e`：1 个真实 Uvicorn walking skeleton 回归通过（完整门槛 e2e 留给 S5）。
- `make demo`：临时库迁移/幂等 seed/health/papers 正常。
- 另以临时 SQLite + 127.0.0.1 Uvicorn 实测合成 G1→G2→种子导入→独立标注→G3：
  种子首次 created=20、再次 created=0/existing=20；校准 20 篇，Spearman=
  0.9694584179118516、Top-5=5、主标签一致率=1、阅读方式一致率=0.95；
  G3 后 dry_run/revision=4，40 篇均 discovered；无评分/入队/推送/归档。
- `make secret-scan`、`git diff --check` 通过；仍有 P1 F5 httpx 弃用警告（非阻断）。
  本轮未验证 Docker/CI，不声称 P2 整体验收。
- SQLite 验证沿用 S3 的 /dev/shm 临时库，以规避当前工作区 fsync 阻塞；未降低
  SQLite 同步设置，也未触碰 var/papersteward.db。复现命令：

  ```bash
  make check PYTEST='.venv/bin/pytest --basetemp=/dev/shm/papersteward-p2-s4-check'
  make test-integration PYTEST='.venv/bin/pytest --basetemp=/dev/shm/papersteward-p2-s4-integration'
  TMPDIR=/dev/shm make migrate-check
  TMPDIR=/dev/shm make e2e PYTEST='.venv/bin/pytest --basetemp=/dev/shm/papersteward-p2-s4-e2e'
  task_demo_dir=$(mktemp -d /dev/shm/papersteward-p2-s4-demo.XXXXXX)
  PAPERSTEWARD_DATABASE_URL="sqlite+pysqlite:///$task_demo_dir/demo.db" make demo DEMO_PORT=8776
  rm -rf "$task_demo_dir"
  make secret-scan
  ```

### S5 可直接使用的输入、限制与回滚

- S5 从 dry_run/revision=4 开始；查最近 onboarding.g3.confirmed 的 evidence 获取
  report/hash、round_number、binding（G2/profile/taxonomy）与 sample_kind。验证前序
  文档仍匹配，试运行参数与日报需要新的版本化证据，不把调用方 passed=true 当校验。
- 合成 `tests/fixtures/calibration/basic.json` 可用于 S5 全链路：拆开 cohort 的建轮
  请求与 agent_batch/user_batch，不把 fixture 用户答案自动复制为真实反馈。种子清单
  沿用 G2 证据；导入是单独显式操作，建校准不依赖导入事件存在。
- 当前尚无试运行日报、G4 确认 API、初始化 CLI/页面、再协商/重置入口；G3 后仍禁止
  正式入队/推送/归档。需要改已选档案/分类法时须在后续设计再协商，不可静默换版本。
- 回滚：停应用/先备份，`alembic downgrade 0003` 删除校准表；Paper/事件/门禁状态
  留存，不能假定 G3 被撤回。完整撤销或回旧代码须恢复 S4 前备份（旧枚举不认识新事件），
  再 upgrade head 恢复工作；不可降级后手改状态绕过门禁。
- 规模超过 ≤8 文件/≤400 行指引：本卡覆盖迁移/ORM/事件/API/纯指标/事务服务、
  独立合成 fixture、59 个测试及必要状态文档；这些共同支持可审计的 G3，未推进 S5。


## S5 验收与 S6 handoff

### 能做什么与 API 契约

- 分支 `feat/p2-s5-dry-run-g4`，基线 S4 `5e843fc`；无依赖/迁移，head 仍为 0004。
  决策见 [ADR 0005](../adr/0005-dry-run-g4.md)。S5 完成至 G4，不宣称 P2 整体验收。
- POST `/onboarding/dry-runs`：仅 dry_run 阶段可创建；version 为 1–9999 的递增
  整数，必须从 1 依次追加，不能覆盖/跳号。输入含 digest_date、sample_kind=synthetic、
  source、parameters、P1 ScoringConfig 和预选 items。样例见
  `tests/fixtures/dry_run/basic.json`；可读输出见同目录 digest.md。
- parameters 必须提供 push_time（HH:MM）、IANA timezone、quiet_start/quiet_end
  （同时为 null 表示关闭）、paused、target_shares、channel=local_markdown、
  external_model_data=disabled|metadata_abstract、share_notes=false。安静时段为左闭
  右开，支持跨午夜，不能包含计划推送时间；目标占比覆盖全部启用主标签、和为 1，
  不静默归一化。不提供备注外发或 SMTP 渠道。
- 每日上限、精读/粗读配额、每标签上限、相关度门槛、权重均复用 P1 ScoringConfig
  校验。S5 主队列容量 5–10、特殊桶容量≤2；items 1–10 篇，不重复、不含未知标签、
  不低于相关度门槛且不超任何配额。条目顺序和分数来自 fixture，未实现生产排序。
  特殊桶容量仅作为待确认参数展示，本次不展示特殊桶条目。
- items 含 ID/title/primary_tag/relevance/read_mode/summary（≤100 字符）/reason；
  不要求这些合成展示元数据等于库中文献，也不写 Paper、Summary、Rating 或 Score。
  日报对不可信标题/概括/理由作文本转义，禁止 HTML、链接或换行注入 Markdown 结构。
- `onboarding.dry_run.created` 事件保存完整输入、G3 event/校准 hash/上游文档绑定、
  完整结构化报告与 Markdown/hash。GET `/onboarding/dry-runs/{version}` 重算校验
  当前依据，报告改变或选定配置版本内容冲突时拒绝。所有日报标为“仅流程验证”。
- POST 同路径 `/feedback`：report_hash、reviewed_by、accepted、issues、可选 comment；
  issues 可用 too_many（太多）、too_few（太少）、inaccurate（不准）、wrong_category
  （分类不对）。未认可须有问题或说明；认可不能含未解决问题。仅能反馈最新日报。
  `onboarding.dry_run.feedback` 事件按独立 sequence 追加；旧意见不覆盖，GET 返回
  feedback_history、最新 feedback 及其 feedback_hash。
- 负面反馈不推进门禁；可修改参数/条目追加新版重新反馈，也可对同版明确重新审阅并
  认可，保留完整历史。最新意见覆盖“当前是否认可”的判断，不删除此前拒绝意见。
- POST 同路径 `/confirm`：confirmed=true、confirmed_by/change_summary、
  expected_revision=4、report_hash、feedback_hash；只接受最新日报与该版最新认可意见。
  新版本或新反馈使旧确认依据失效；缺 G3/前序漂移/未认可/配置冲突均不激活。
- G4 同事务保存 scoring 选定版本（已有版本必须完全相同，否则拒绝）、
  Config(key=onboarding_runtime, version=日报整数的字符串, value_json=parameters)、
  config.updated 事件、G4 确认快照和 active/revision=5。预览不写 Config；审计失败
  不残留配置或半激活状态，支持调用方捕获失败后提交/外层事务回滚。
- G4 evidence 含 version、完整 report/hash、feedback/hash/history、scoring_version
  与 runtime_version；G4 产物 artifact_id=dry-run / artifact_version=日报版本字符串。
  这些引用是选定配置的依据，不可用另一个文档 ID 的 latest 或 ConfigRepository.latest
  替代。已有更新日期更晚的其他 scoring 版本不会改变本次明确选定的版本。

### 验收证据（2026-10-21）

- `make check`：ruff format/check、strict mypy（65 源文件），539 个非 e2e 测试通过。
- `make test-integration`：198 个通过；本轮新增 54 项测试（29 unit、24 integration、
  1 e2e）。覆盖参数时间/占比/配额/权重校验、日报 golden 与文本注入、四类负面反馈、
  最新认可、版本不可覆盖、过期双 hash/revision、G3/档案/分类法/论文/校准/日报/配置
  漂移、审计冲突/时间倒退、G4 配置与状态原子回滚，以及已有评分版本复用。
- `make e2e`：2 个通过（P0 walking skeleton + 新 G1–G4 链路）。新测试使用真实
  localhost Uvicorn 和迁移后的临时 SQLite，依次核对 seeds/profile/taxonomy/
  calibration/dry_run 阶段的真实 PaperRepository 入队、推送、归档均抛 G4 错误且
  不写事件；拒绝未认可/负面反馈，调整为日报 v2 并认可后通过 G4；ACTIVE 后真实
  仓储迁移允许，测试回滚探针操作，不产生实际发送或永久测试归档。
- 当前没有 enqueue/push HTTP 端点；e2e 不把 404 当门禁证据，也未新增虚构发送端点。
- `make migrate-check`：base→0004→base→0004 通过；模型一致性集成测试通过。
- `make demo`：临时库迁移/seed/health/papers 正常；`make secret-scan` 与
  `git diff --check` 通过。Docker/CI 本轮未重跑；保留 P1 F5 httpx 弃用警告。
- 延续前序 /dev/shm 测试库避免当前工作区 fsync 阻塞，未改 SQLite 同步设置，未触碰
  var/papersteward.db。可复制验收：

  ```bash
  make check PYTEST='.venv/bin/pytest --basetemp=/dev/shm/papersteward-p2-s5-check'
  make test-integration PYTEST='.venv/bin/pytest --basetemp=/dev/shm/papersteward-p2-s5-integration'
  TMPDIR=/dev/shm make migrate-check
  TMPDIR=/dev/shm make e2e PYTEST='.venv/bin/pytest --basetemp=/dev/shm/papersteward-p2-s5-e2e'
  task_demo_dir=$(mktemp -d /dev/shm/papersteward-p2-s5-demo.XXXXXX)
  PAPERSTEWARD_DATABASE_URL="sqlite+pysqlite:///$task_demo_dir/demo.db" make demo DEMO_PORT=8777
  rm -rf "$task_demo_dir"
  make secret-scan
  ```

### S6 可直接使用的输入、限制与回滚

- S1–S5 的 API 完整覆盖 start/status、档案/G1、分类法/G2、独立校准/G3、参数与
  试运行反馈/G4；下一步只做薄 CLI/页面与阶段验收，不复制校验器或新增直通门禁。
- 最小客户端按响应 stage/next_gate/revision 驱动，编辑时用新版本；确认必须让用户
  审阅真实返回的全文/参数/报告，原样回传 hash，不把测试认可操作自动用于用户数据。
- `tests/e2e/test_onboarding_gates.py` 是可直接复用的全链路示例；三套合成 fixture
  分别提供 taxonomy、calibration、dry_run 输入。真实用户校准仍须独立标注。
- ACTIVE 只表示门禁确认，不启动抓取、调度、发信或外部模型调用。后续 P3/P6
  调度/发送器必须读取 G4 选定版本并执行暂停/安静时段/配额/渠道/隐私规则。
  本轮未实现真实来源、摘要/评分、正式推送/归档编排、特殊桶、鉴权、再协商/重置。
- 无 schema 回滚；完整撤销激活或退回旧事件枚举代码须停应用并恢复 S5 前备份。
  仅回代码不会撤销 Config/ACTIVE，不可降级 0004 或手改状态充当 S5 回滚。
- 规模超过 ≤8 文件/≤400 行指引：参数/渲染、事件版服务、API、合成输入与 golden、
  54 项测试及必要状态/ADR/交接文档共同构成 G4 竖切，未推进 S6 或生产算法。


## S6 验收与 P2 收尾（2026-10-22）

- 基线 S5 `72336df`；分支 `feat/p2-s6-onboarding-cli`。最小体验采用本地 HTTP CLI，
  注册到现有 papersteward 入口；无新增依赖/迁移，head 0004，未实现网页或 P3 算法。
- 命令：onboarding start/status/import-seeds、submit/review（profile/taxonomy/
  calibration/dry-run）、label agent/user、feedback、confirm。仅调用 S1–S5 API，
  不连接数据库，不读服务器 .env，不复制可信业务校验器；P1 通用文档 CLI 保持不变。
- 可读输出展示全文、差异、缺项/协商问题、指标及日报；--json 提供结构化输出。
  不可信文档中的终端控制字符按文字转义，不执行论文内容。
- --out 保存 0600、不覆盖已有文件的完整已审快照，包含服务器地址、阶段/revision、
  产物与 SHA256 校验和。确认要求 --gate/--review/--by/--summary/--yes；只回传旧快照
  的 hash/revision，不 GET 刷新或重试 POST。G3 可显式接受书面偏差；G4 需先明确认可。
  校验和仅检测意外改动，不替代鉴权；仍为本地单用户。
- 地址限 loopback，禁用代理/重定向，10 秒超时。错误非零退出并提示修复/重新审阅；
  本地保存失败明确服务端可能已成功，使用 review 恢复，禁止自动重复 POST。
- 完整操作指南见 [onboarding-cli.md](../onboarding-cli.md)，决策见
  [ADR 0006](../adr/0006-onboarding-cli.md)。合成 fixture 只用于演示，不自动用于真实标注。

### 最终验证

- `make check`：ruff format/check、strict mypy（67 源文件），**567 passed**（3 e2e 排除）。
- `make test-integration`：**209 passed**；`make e2e`：**3 passed**。
- S6 新增 **29** 项（17 unit、11 integration、1 e2e）：本地 URL/错误信封、快照校验/
  0600/跨服务器拒绝、未明确确认、只用旧依据/拒绝重复确认、全文/diff/终端转义、
  保存失败恢复、参数错误/输出冲突时不发送变更，以及真实 CLI 的完整 G1–G4。
- 真实 CLI e2e 在独立临时库启动 Uvicorn，用实际 papersteward 子进程跑通所有门，
  环境代理故意指向不可用端口仍能成功；确认负面反馈拒绝后再明确认可，最终
  active/revision=5，仅保存两份配置，未写 Rating/ScoreSnapshot/ArchiveSnapshot。
- `make migrate-check`：base→0004→base→0004 通过；`make demo DEMO_PORT=8778`：
  临时库迁移/seed/health/papers 通过；`make secret-scan`、`git diff --check` 通过。
- `docker compose config --quiet` 通过；Docker daemon socket 权限拒绝，sudo 被
  no-new-privileges 禁止，未运行 build/up。CI 本轮未触发；不能据 P1 证据声称本轮
  Docker/CI 通过，暂不打 p2-done tag。保留 P1 F5 httpx 弃用警告及 F3 非阻断测试债。
- 本轮沿用 /dev/shm 临时测试库避免工作区 SQLite fsync 阻塞，未降低同步级别，未触碰
  var/papersteward.db。完整验收矩阵见 [P2-acceptance.md](P2-acceptance.md)。

```bash
make check PYTEST='.venv/bin/pytest --basetemp=/dev/shm/papersteward-p2-s6-check'
make test-integration PYTEST='.venv/bin/pytest --basetemp=/dev/shm/papersteward-p2-s6-integration'
TMPDIR=/dev/shm make migrate-check
TMPDIR=/dev/shm make e2e PYTEST='.venv/bin/pytest --basetemp=/dev/shm/papersteward-p2-s6-e2e'
make secret-scan
```

### 能力、边界与回滚

- P2 的档案/分类法/校准/参数/试运行/反馈/确认已形成 API 与 CLI 完整链路；G4 前
  所有仓储入队/推送/归档路径仍拒绝。ACTIVE 仅表示门禁已确认，不启动外部调用。
- 当前没有真实文献源/真实模型、生产排序评分/摘要、日报发送/调度、网页、鉴权、
  方向变更后的再协商/reset。合成指标仅流程验证。后续消费参数必须处理暂停/安静时段。
- 无 S6 schema 回滚；回退 CLI 不撤销已完成的文档/配置/状态。撤销初始化须停应用并
  恢复操作前备份；不能删除快照/降级迁移/手改门禁冒充安全恢复。
- 文件/行数超过 ≤8/≤400 指引：CLI/传输/审阅快照、29 项测试、完整操作指南与
  P2 验收/状态/交接共同构成本阶段交付；无无关重构，未提前实现 P3。

## P3 handoff：可直接开始

1. **先拆 P3 卡片**：阅读计划 P3 与设计采集章节，建立 docs/phases/P3-ingestion.md
   的 5–8 个小阶段（adapter 契约、去重/硬过滤、来源适配器等）；本轮没有开始 P3。
2. **精确文档与配置引用**：从 onboarding.g1/g2.confirmed 的 artifact_id/version
   读取已选 confirmed Profile/Taxonomy；从 G3 evidence 获取独立校准报告/hash；
   从 G4 evidence 获取完整报告/feedback、scoring_version、runtime_version。
   scoring 用 Config(key=scoring, version=evidence.scoring_version)，运行参数用
   Config(key=onboarding_runtime, version=evidence.runtime_version)。不要用无关 ID
   的 latest_confirmed 或 ConfigRepository.latest 代替明确选定版本。
3. **门禁与流水线**：复用 get_onboarding/ensure_active；真实发送器和入队/归档都必须
   经统一门禁。采集只从 discovered 开始，依正式状态机进入 deduped/screened，不能
   从采集跳到队列。现有 G4 放行不代表配额/暂停/隐私等生产规则已实现。
4. **数据库**：head 0004。P1 14 张领域表加 onboarding/onboarding_calibration；
   后续字段/表变更必须新增可逆迁移，保留事件/文档版本契约与外部事务边界。
5. **离线输入与测试**：calibration/basic.json、dry_run/basic.json 与 digest.md，
   加 taxonomy/onboarding.md 可重放全部确认链路。所有真实来源适配器测试用
   cassette/fake，数据库 tmp_path；live 测试显式手动，不加入 CI。
6. **已有限制**：P1 PaperTag 无 taxonomy_id，队列影响仅候选预览；无方向变更/reset
   契约，不能静默替换已选版本。真实模型/摘要/评分属于 P4/P5，不在 P3 顺手引入。
7. **验收遗留**：Docker 与 CI 已独立验证（P2 CI run 39643102708）；阶段 tag 留待发布流程。
   P1 F5/F3 债务仍记录，不阻塞当前本地代码交接。未发现本轮引入的 P0/P1 阻断缺陷。
