# P3 采集、去重与硬过滤

> 依据：大阶段开发计划 §3 P3、§5.3；设计 §6；P2 handoff。
> 状态：P3-S1–S7 实现与本地阶段门完成；Docker/CI 已独立验证，P3 验收通过（2026-10-22）。
> 验收后四项非阻断优化已跟进（状态收敛、缓存字节上限、OpenAlex 身份分页、CLI 多页
> fixture）。独立报告见 [P3-acceptance.md](P3-acceptance.md)。
> 前置：P1/P2 当前 checkout 为 stacked 基线；P2 CI 已由后续 run 补齐，P2 tag 仍待发布流程。

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

## S4 handoff（2026-10-22）

- 分支 feat/p3-s4-openalex-adapter（stacked S3）；无新依赖/迁移，共6文件。
- 实现 OpenAlex 搜索、日期过滤、保守100条基础分页、倒排摘要重建、作者/venue/引用/
  OA/语言/类型映射；缺失字段为 None/()/unknown，保留完整 work 原始元数据。
- 字段查询先解析作者/source/institution 的精确名称到唯一 ID；没有匹配为空结果，
  同名歧义拒绝 unsupported_query，不擅自选人/机构；单个字段多个关键词也显式拒绝。
- 查询字符串按字面短语引用；分页保持 per_page 恒定并裁切末页，重复页/损坏结构拒绝。
- 官方依据（2026-10-22 查阅）：[基础分页](https://help.openalex.org/api/paging/)、
  [搜索](https://help.openalex.org/api/searching/)、[字段](https://help.openalex.org/data/works/attributes/)。
  旧 docs 域已迁移 help；不依赖已弃用200条页或需要密钥的跨页cursor。没有真实API调用。
- 能做：离线元数据召回；不能做：生产入库/富化评分或同名实体自动消歧。
  publication_date 是来源报告的主版本日期，不保证所有 locations 中最早；S6 跨源取最早。
- 演示：`.venv/bin/pytest tests/contract/test_openalex_adapter.py -q`（13项）；
  make check（ruff/strict mypy，639 passed）、test-integration（210 passed）、
  migrate-check（base↔0004）、secret-scan 与 diff --check 通过；使用独立 /dev/shm 临时库。
  回滚代码即可；DB head 0004。

## S5 handoff（2026-10-22）

- 分支 feat/p3-s5-semantic-scholar（stacked S4）；ADR 0010，无新依赖/迁移。
- Semantic Scholar bulk search 编译字面短语与 +/|/-，日期闭区间、token 分页、总结果
  上限；保留引用数/外部ID/TLDR/raw metadata。TLDR不当摘要，缺精确日期不补1月1日。
- 只支持 all 查询，字段关注列表显式 unsupported_query；不在第三来源重复做身份解析。
  可选 SecretStr key 只放 x-api-key，HTTP client 绑定本来源host，禁止跨来源泄漏。
- 官方接口依据：[Academic Graph API](https://api.semanticscholar.org/api-docs)，2026-10-22
  查阅。没有真实key/来源调用；公共接口401/429明确失败，跨源隔离留S7。
- 演示：pytest tests/contract/test_semantic_scholar_adapter.py；新增11项契约案例。
  能做：离线召回与来源元数据解析；不能做：引文网络/摘要生成/生产入库。
- 回滚本阶段代码即可；DB head0004。验收：make check（ruff/strict mypy，650 passed）、test-integration（210 passed）、
  migrate-check（base↔0004）、secret-scan、diff --check 通过，沿用 /dev/shm 临时测试库。

## S6 handoff（2026-10-22）

- 分支 feat/p3-s6-canonical-dedupe（stacked S5），ADR 0011，无依赖/迁移。
- DOI/arXiv/标题/作者年份venue/摘要近似五类去重，记录原因与全部provenance；
  正式版字段优先、保留最早日期，稳定 canonical ID；字面摘要5词shingle替代embedding，
  不声称语义模型已实现。强ID冲突阻止弱匹配，无ID桥接不得串联两个已知冲突组。
- API：canonicalize、duplicate_reason、deduplicate → DedupeGroup（candidate/members/
  reasons/provenances/canonical_id）。纯转换，不改状态/库/归档。S7仍须先查已有库内
  文献与source记录，不能仅按新canonical_id创建，避免新DOI导致重复。
- 演示：pytest tests/unit/test_dedupe.py tests/contract/test_canonical_candidates.py；10项
  案例含跨三源/版本合并/反例/强ID桥接/顺序稳定性。未实现跨语言语义相似或大规模索引。
- 规模：7文件、395行，符合小阶段指引；无S7代码。
  回滚本次代码即可。make check（ruff/strict mypy，660 passed）、test-integration（210 passed）、
  migrate-check（base↔0004）、secret-scan、diff --check 通过，沿用 /dev/shm 临时测试库。

## S7 handoff 与 P3 收尾（2026-10-22）

- 分支 feat/p3-s7-ingest-reports（stacked S6），ADR 0012；任务A（过滤/来源表）与B
  （编排/CLI/阶段门）均已实现。无新依赖，head0005新增paper_source；EventType追加
  paper.metadata_updated，既有值不改名。迁移up/down保持paper/run/event数据。
- 生产与dry-run均绑定精确G1/G2/G4及G3校准/报告/反馈，读取选定scoring/runtime而非latest；
  preflight及落库前复核。暂停/安静时段拒绝，未读取/外发备注，模型/推送配额保持原契约。
- 候选经去重、硬过滤后按discovered→deduped→screened/rejected写入，逐步追加事件；
  原始来源及版本以source/id/raw内容哈希追加。重跑不增相同Paper/来源版本，来源更新
  保留库内ID；只有ingest创建且仍处前期状态的元数据可补充，已评分/归档/拒绝和
  种子/校准记录不改写。无评分/分类/入队/发送/归档。
- 独立来源错误、请求/缓存数、重复、过滤原因、耗时及版本绑定进入Run；部分成功
  stats.partial，全部失败为failed。savepoint撤销半批状态/来源/事件但保留失败报告，
  外层session_scope仍能全部回滚；仓储不自行commit。dry-run所有表只读。
- CLI ingest支持单/多来源fixture与明确--live、字面query、结果上限、dry-run；
  runs show按ID或--last查看JSON报告。完整指南见 [ingestion-cli.md](../ingestion-cli.md)。
- 定向案例覆盖真实G4、重复运行、preflight门禁、暂停/安静时段、所选配置与漂移、
  来源超时隔离、全失败、审计/外层回滚、硬过滤原因落库、版本更新与归档/评分保护。
  真实CLI+Uvicorn测试在完整G1–G4后连续采集两次，新增数1→0、来源记录不重复。
- 规模超出8文件/400行：过滤、来源迁移/仓储、精确配置消费、跨源事务编排、CLI及完整
  回归/迁移/e2e证据共同实现S7竖切；小任务A/B均保留在同一S7分支，没有提前实现P4。
  S7整合时修正短标题无作者无ID的fallback ID碰撞，新增回归；不属于去重策略扩张。
- 回滚：停采集并备份；0005→0004仅删除来源表。完整撤销新增文献/报告/审计或退回
  不认识新事件枚举的代码须恢复S7前备份；不能仅降迁移冒充数据撤销。
- 最终验证：新增24项测试；make check（ruff format/check、strict mypy 80源文件，
  **684 passed**）；make test-integration（**224 passed**）；make e2e（**4 passed**）。
  make migrate-check（base→0005→base→0005及模型一致性）、独立临时库make demo
  DEMO_PORT=8780（health/papers）、secret-scan、diff --check均通过。未触碰var数据库，
  未使用真实网络/LLM/用户密钥进行测试；保留上游Starlette/httpx弃用警告。

```bash
make check PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p3-s7-final-check'
make test-integration PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p3-s7-final-integration'
make e2e PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p3-s7-final-e2e'
TMPDIR=/dev/shm make migrate-check
make secret-scan
```

本地分支/提交链：S2 `92c00d0` → S3 `3336393` → S4 `08c5169` → S5 `0e3271a`
→ S6 `eb23cd8` → S7 `a93d96a`。P3-S7 已合并到 main（`9fe91dc`）；Docker/CI
已独立验证（branch run 39709657832、main run 39709826878）。P3 阶段 tag 仍待发布
流程，不据此声称已发布 Alpha。

## P3 验收后非阻断优化（2026-10-22）

依据 [P3-acceptance.md](P3-acceptance.md) §5 的四项低优先级观察完成优化；未变更 schema、
执行门禁、来源契约或 P4 边界：

1. **早期状态收敛**：重跑时，由 ingest 创建且仍处 `discovered`/`deduped` 的文献在无新
   过滤原因时按状态机补齐 `deduped → screened`；出现新原因时进入 `rejected`。事件链与
   新建文献一致；归档/已拒绝/已评分/种子/校准记录仍不改写。
2. **缓存字节上限**：`SourceHTTP` 缓存仍最多 128 条，同时受 32 MiB 总字节限制，按 FIFO
   淘汰；单响应仍限 8 MiB，超过总字节预算的单条响应只返回不缓存。新增 `cache_bytes`
   统计供测试/诊断。
3. **OpenAlex 身份消歧分页**：作者/venue/机构精确名称解析由只读第一页改为最多 3 页
   （300 条）的保守分页；仅当完整扫描且精确匹配唯一时解析为 ID，同名多匹配或未完整
   扫描仍返回 `unsupported_query`，不猜人/机构。
4. **CLI 多页 fixture**：`--fixture` 文件可为原始响应（继续重复回放），也可为
   `{"responses": [对象/字符串, ...]}` 信封，按 HTTP 请求顺序回放多页响应；列表耗尽返回
   明确的 400 而不是重复最后一页。多页 arXiv/OpenAlex/Semantic Scholar cassette 可按
   请求顺序端到端演示。

验证：新增/更新回归覆盖状态收敛、缓存条数/字节淘汰、身份消歧分页/歧义/缺 meta、fixture
顺序回放/耗尽/非法信封。阶段门结果见下方“P3 优化验证”。回滚：四项均为本地代码/文档
改动，无迁移、无依赖、无持久化格式变更；撤销提交即可，既有运行报告与来源记录无需迁移。

### P3 优化验证

- `make check PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p3-opt-make-check'`：
  ruff format/check、strict mypy（80 源文件）通过；**700 passed / 4 e2e excluded**
  （较验收基线 684 新增 16 项）。
- `make test-integration PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p3-opt-integration'`：
  **228 passed**（较验收基线 224 新增 4 项，含真实 CLI 多页 fixture）。
- `make e2e PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p3-opt-e2e'`：**4 passed**。
- `TMPDIR=/dev/shm make migrate-check`：base → 0005 → base → 0005 通过。
- `make demo DEMO_PORT=8055`：临时库迁移/seed、`/health`、`/papers` 正常。
- `make secret-scan`、`git diff --check` 通过；未使用真实网络/LLM/用户密钥。

本轮仅改代码、测试与文档，**未新增 schema/迁移/依赖/容器配置**，head 仍为 `0005`；
未重跑 Docker/CI，因此不把本轮说成新的 Docker/CI 验收。P3 验收报告中的 Docker/CI
证据仍对应 `a93d96a`（main `9fe91dc`）。回滚方式见上。

## P4 handoff（P4-S1–S5 已完成）

- P4-S1–S5 已完成真实 OpenAI-compatible provider/配置入口、富化输入契约、
  `enrichment` 持久化、相关度 R、100 字摘要与分类/标签桶，见
  [P4-enrichment.md](P4-enrichment.md)；P4-S6 特殊桶提名待开始。
- 输入：SourceAdapter/QuerySpec/PaperCandidate、三个来源cassette、canonicalization/
  DedupeGroup、screened候选池、paper_source原始记录/候选JSON、run报告与选定文档/配置绑定。
- P4 已拆 8 张卡；继续使用FakeLLM/cassette，不因ACTIVE擅自调用真实模型。
  精确读取G1/G2/G4选定版本；新字段/表走新增迁移（当前0006），不能重写旧迁移。
- 能做：手动离线/明确live采集、去重/硬过滤、可审计运行报告和CLI；不能做：自动调度、
  embedding语义去重、摘要/分类/评分、生产队列/推送/归档、再协商/reset。
- 已知边界：词法摘要去重可能漏掉翻译/重写；过滤清单由已确认档案显式提供，未知语言/
  日期不伪造。持久化失败只保存脱敏pipeline_failed报告；来源API变更需重放cassette定位。
- P1 F5弃用警告/F3非阻断测试债仍保留；P2/P3 CI 已通过，阶段 tag 仍待发布流程。
  本轮无真实API/LLM/用户密钥调用。
