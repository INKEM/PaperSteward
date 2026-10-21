# P3 采集、去重与硬过滤

> 依据：大阶段开发计划 §3 P3、§5.3；设计 §6；P2 handoff。
> 状态：P3-S1–S3 完成并通过本地验收；S4–S7 未开始。日期：2026-10-22。
> 前置：P1/P2 当前 checkout 为 stacked 基线，P2 CI/tag 证据仍待补。

## 小阶段卡片

每阶段独立分支 `feat/p3-s{n}-{slug}`；测试默认离线，数据库 tmp_path；各阶段都执行
`make check`、`make test-integration`、`make migrate-check`，结束大阶段再核对全部阶段门。
以下路径为执行目标，后续阶段开工时在本文件细化，不提前实现。

### P3-S1：SourceAdapter 契约与 fixture harness（本次）

- 目标：固定查询输入/统一元数据/来源错误，通过假 adapter 重放并演示与现有 API 兼容。
- 输入/依赖：P1 Paper/枚举、P2 门禁与 handoff；本阶段创建合成来源 fixture。
- 输出：sources/contracts.py、sources/fixtures.py、fixtures/sources/basic.json；
  unit/test_source_contracts.py、contract/test_source_adapter.py、现有 walking skeleton e2e。
- 契约：见 ADR 0007；search 返回 list[PaperCandidate]，无结果为 []，来源失败为
  SourceError；无库内 ID/状态/分数，缺字段降级，保留独立来源溯源。
- 测试：校验正反例、确定性回放/上限/空结果/损坏文件/来源不一致/超时；
  假 adapter → 测试侧 Paper 投影 → 临时迁移库 → 真实 Uvicorn `/papers`。
- 验收：下方演示及 unit/contract 命令、make check/test-integration/migrate-check/e2e。
- 范围外：真实 API/HTTP 客户端、查询生成、去重/过滤、生产 ingest/来源持久化/评分。
- 风险/回滚：外层模型冻结不等于嵌套 JSON 深度不可变；撤销代码即可，无迁移/新依赖。
- 预估：1 天；交付代码、测试、fixture、ADR、handoff、CHANGELOG。

### P3-S2：从已确认 profile 生成查询

- 目标/依赖：基于 S1 与 G1 精确选定的 Profile 生成稳定关键词/布尔/关注列表输入。
- 输入：G1 artifact_id/version 对应 confirmed Profile、查询预算；不使用无关 latest。
- 输出：sources/queries.py、unit/test_source_queries.py、profiles/basic.md 用例。
- 契约：稳定顺序、排除词、空/未确认档案明确报错；扩展 QuerySpec 时更新 ADR/契约测试。
- 验收：表驱动正反例；`.venv/bin/pytest tests/unit/test_source_queries.py -q`。
- 范围外：网络/embedding/引文扩展；回滚：撤销查询模块；预估 0.5–1 天。

### P3-S3：arXiv 适配器与 HTTP 边界

- 目标/依赖：S1/S2 查询经 arXiv 搜索/解析/分页转换为候选并保留来源。
- 输入：QuerySpec、合成 Atom/HTTP cassette；可选手动 live，不加入 CI。
- 输出：sources/arxiv.py、sources/http.py、contract/test_arxiv_adapter.py、sources/arxiv.xml。
- 契约：超时/退避/限流/请求预算/User-Agent/缓存；遵守总结果上限，失败 SourceError；
  httpx 从 dev 提升为 runtime 前记录 ADR；API 规则以当时官方文档为准。
- 验收：分页、空/损坏响应、缺字段、429/超时；pytest tests/contract/test_arxiv_adapter.py。
- 范围外：PDF/去重/生产 ingest；回滚：移除来源注册/模块；预估 1–2 天。

### P3-S4：OpenAlex 适配器

- 目标/依赖：S1–S3 公共 HTTP 边界，从 OpenAlex cassette 生成候选。
- 输入/输出：QuerySpec → sources/openalex.py、contract/test_openalex_adapter.py、sources/openalex.json。
- 契约：缺 venue/摘要/引用数降级，最早公开时间/DOI/OA/作者与 provenance 保留。
- 验收：分页、字段缺失、倒排摘要、限流失败；pytest tests/contract/test_openalex_adapter.py。
- 范围外：评分/富化编排；回滚：移除来源注册/模块；预估 1–2 天。

### P3-S5：Semantic Scholar 适配器（可后置）

- 目标/依赖：S1–S4，第三来源共享协议与 HTTP 边界；至少前两个来源完成才可裁剪。
- 输入/输出：QuerySpec → sources/semantic_scholar.py、对应 contract 测试与来源 cassette。
- 契约：认证不写日志/cassette，摘要/TLDR/引用缺失降级，失败只影响该来源。
- 验收：pytest tests/contract/test_semantic_scholar_adapter.py；空结果/分页/认证失败。
- 范围外：真实密钥/引文图；回滚：移除注册/模块；预估 1–2 天。

### P3-S6：canonicalization 与去重

- 目标/依赖：S1 候选、S3/S4 跨源 fixture，输出可审计的 canonical 与重复结果。
- 输入/输出：候选及现有指纹 → pipeline/dedupe.py、unit/test_dedupe.py、重复案例 fixture。
- 契约：DOI/arXiv/标题/作者年份/摘要近似五类；保留最早日期与各来源，正式版信息优先；
  无真实 embedding 的离线近似策略及阈值须先记 ADR，不声称语义模型已实现。
- 验收：pytest tests/unit/test_dedupe.py；跨源/版本合并/误合并反例/确定性重跑。
- 范围外：评分/归档更新；回滚：撤销纯转换模块；预估 1–2 天。

### P3-S7：硬过滤、运行报告与 ingest

- 目标/依赖：S1–S6，候选经去重/硬过滤幂等入库并可审计失败。
- 输入：G1/G2/G4 精确选定版本、来源候选、档案过滤条件、既有文献指纹。
- 输出：pipeline/screen.py、pipeline/ingest.py、CLI ingest/runs、RunRepository、
  来源溯源持久化与所需可逆迁移、unit/integration/e2e 测试（如超规模再拆卡）。
- 契约：语言/日期/类型/排除词/黑名单/已归档与已拒绝；discovered→deduped→screened；
  不越过 G4 入队，执行暂停/隐私/预算参数；重跑幂等，失败报告不伪装成功。
  RunStatus 保持 P1 冻结值，跨源 partial 用报告统计表达，必要变更先 ADR。
- 验收：过滤表驱动与失败报告、事务回滚、迁移 up/down、临时库 ingest 两次、runs show，
  make check/test-integration/migrate-check/e2e/demo/secret-scan；真实来源测试仍离线。
- 范围外：相关度模型/摘要/分类/评分/正式推送；回滚：停采集并恢复备份后降迁移；1–2 天。

## S1 契约、演示与 handoff

接口说明及决策见 [ADR 0007](../adr/0007-source-adapter-contract.md)。合成 fixture
不是来自 arXiv/OpenAlex 的真实响应；真实解析证据在 S3/S4 补齐。

```bash
.venv/bin/pytest tests/unit/test_source_contracts.py tests/contract/test_source_adapter.py -q
.venv/bin/python -c 'from pathlib import Path; from papersteward.sources.fixtures import FakeSourceAdapter; from papersteward.sources.contracts import QuerySpec; a = FakeSourceAdapter(Path("tests/fixtures/sources/basic.json")); print([p.model_dump(mode="json") for p in a.search(QuerySpec(keywords=("retrieval",), max_results=1))])'
```

- 能做：验证查询/候选、重放固定候选/失败、保留来源原始记录与 UTC 时间；无网络依赖。
- 不能做：生产采集/来源持久化、查询匹配、HTTP、canonicalization、过滤/入队/推送。
  API e2e 中投影/写库为测试准备，不能据此声称生产 ingest 或 DB provenance 已完成。
- 数据库/门禁：head 0004；P1 实体与 G4 门禁未变，无用户数据变更或后台任务。
- S2 输入：QuerySpec/SourceAdapter/PaperCandidate；扩展查询语义须更新契约。仍按 P2
  handoff 精确读取确认版本；P2 CI 未触发、P1 F5/F3 债务保留。
- 回滚：撤销本次代码与文档；无 schema/运行状态回滚。
- 规模说明：为首次建立 P3 的 7 张卡、协议/回放、分层测试、ADR 与状态文档，超过
  ≤8 文件/≤400 行指引；没有推进 S2–S7 或修改已有业务实现。

### S1 验证证据

- 分支 `feat/p3-s1-source-contract`；本轮新增 33 项（21 unit、11 contract、1 e2e）。
- `make check`：ruff format/check、strict mypy（69 源文件）通过；**599 passed**，
  4 e2e 排除。`make test-integration`：**209 passed**。
- `make e2e`：**4 passed**，包含新增 fake 来源兼容性演示；真实 localhost Uvicorn，
  独立迁移临时库，API 显示固定来源候选为 discovered，未绕过任何执行门禁。
- `make migrate-check`：base→0004→base→0004 通过；`make demo DEMO_PORT=8779`：
  独立临时库迁移/seed/health/papers 通过；上方离线回放演示输出正确候选与溯源。
- `make secret-scan`、`git diff --check` 通过。保留上游 Starlette/httpx 弃用警告；
  无本轮发现的阻断缺陷。本轮未运行 Docker/CI，不代表整个 P3 阶段门完成。
- 测试沿用 P2 的 /dev/shm 临时库方式，未降低 SQLite 同步级别、未触碰 var 数据库；
  真实 Uvicorn 测试需要允许 localhost socket（受限沙箱首次运行被权限阻止，放行后通过）。

```bash
make check PYTEST='.venv/bin/pytest --basetemp=/dev/shm/papersteward-p3-s1-check'
make test-integration PYTEST='.venv/bin/pytest --basetemp=/dev/shm/papersteward-p3-s1-integration'
make migrate-check
make e2e PYTEST='.venv/bin/pytest --basetemp=/dev/shm/papersteward-p3-s1-e2e'
task_demo_dir=$(mktemp -d /dev/shm/papersteward-p3-s1-demo.XXXXXX)
PAPERSTEWARD_DATABASE_URL="sqlite+pysqlite:///$task_demo_dir/demo.db" make demo DEMO_PORT=8779
rm -rf "$task_demo_dir"
make secret-scan
```

## S2 handoff（2026-10-22）

- 分支 feat/p3-s2-query-generator（stacked 在 S1），无依赖/迁移。ADR 0008 冻结
  any/all、字面排除短语、all/author/venue/team 查询字段；子方向按优先级稳定排序，
  methods/data/关注列表补充召回，保序去重、预算截断，空输入/草案/漂移明确拒绝。
- selected_profile 根据 G1 event 的精确 ID/version 取档案并核对正文/解析缓存；
  不受无关已确认档案影响。生成函数不调用网络/LLM，也不改变档案或门禁。
- unit + integration 定向演示命令：
  `.venv/bin/pytest tests/unit/test_source_queries.py tests/integration/test_profile_query_selection.py -q`。
- 能做：离线生成来源无关查询；不能做：真实查询编译/检索/语义扩写/入库。
  回滚代码即可；S3 消费 QuerySpec 并转义字面值，不能将字段当原生查询指令。
- 验证：定向 8 项通过；make check（ruff/strict mypy，607 passed）、
  make test-integration（210 passed）、make migrate-check（base↔0004）、secret-scan、
  diff --check 全部通过；使用 /dev/shm 独立测试目录，未触碰 var 数据。
  本阶段 8 个文件；S1 API e2e 未受影响，P3 收尾再统一执行 e2e/demo。

## S3 handoff（2026-10-22）

- 分支 feat/p3-s3-arxiv-adapter（stacked S2）。已先写 ADR 0009 并将已有 httpx 从 dev
  提升 runtime，无新第三方库、无迁移。官方 API 手册已查阅，未进行真实来源调用。
- SourceHTTP 提供超时、串行限流、有限退避、Retry-After、请求预算、禁代理/重定向、
  HTTPS host 白名单、8MiB 响应上限和有界24小时内存缓存；统计请求/缓存命中。
- arXiv 查询编译支持字段/布尔/字面排除/UTC日期闭区间，Atom 解析和分页遵守总结果
  上限；首次公开日与更新日不混淆，保留版本 ID/entry XML/作者/DOI/链接。team 或
  含无法安全引用字符的查询为 unsupported_query；预算耗尽为 budget_exceeded。
- 能做：离线 MockTransport 重放真实结构 Atom 与公共 HTTP 失败策略；不能做：
  生产调度/入库/去重/持久缓存/跨进程限流。调用方须 close HTTP client。
- 演示：`.venv/bin/pytest tests/contract/test_arxiv_adapter.py tests/unit/test_source_http.py -q`。
- 超过8文件/400行指引：公共 HTTP 与 arXiv 协议、依赖ADR、离线正反例及必要交接
  共同构成本阶段；没有实现 S4。回滚代码/runtime依赖声明即可，DB head仍0004。
- 验收：新增19项离线测试；make check（ruff/strict mypy，626 passed）、
  make test-integration（210 passed）、migrate-check（base↔0004）、secret-scan 与
  diff --check 通过。临时库沿用 /dev/shm；未运行 live、Docker/CI。
