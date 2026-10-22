# P4 富化、摘要、分类与静态评分

> 依据：大阶段开发计划 §3 P4、§5.3；设计 §6.5–6.7、§10。
> 状态：**P4-S1 完成并通过本地阶段门；P4-S2–S8 未开始。**
> 前置：P3 验收通过；真实 LLM 接入不改变 P3 采集、去重与 G4 门禁。

## 小阶段卡片

P4 是最大阶段，拆成 8 张卡；每张卡单独分支/提交，默认离线测试。原计划 P4-S1
“富化数据模型与 provider”同时包含真实网络接入和 schema 变更，拆成两张卡以控制
单卡规模：S1 冻结真实 LLM 配置/provider 与富化输入契约，S2 再落富化持久化。

### P4-S1：真实 LLM provider、配置入口与富化输入契约（本次）

- 目标：提供可配置的 OpenAI-compatible 真实 LLM 入口，并冻结富化 provider 的
  中性值输入/输出契约；默认仍为 FakeLLM。
- 输入/依赖：P1 的 `LLMProvider`/`LLMGateway`/FakeLLM；P3 的 `Paper`、来源元数据；
  `Settings` 配置体系。
- 输出：
  - `Settings` 的 `PAPERSTEWARD_LLM_*` 字段、校验与 `.env.example`；
  - `llm/openai_compatible.py`（httpx `POST /chat/completions`，硬超时、usage/成本、
    脱敏错误）、`llm/factory.py`（显式 `live` 授权门）；
  - `pipeline/enrich.py`（`EnrichmentSignals`、`EnrichmentProvider`、`NeutralEnricher`）；
  - `papersteward llm ping --fake|--live`；
  - ADR、配置指南、单元/集成 CLI 测试。
- 契约：
  - 默认 `llm_provider=fake`；真实调用必须显式 `--live`，不允许隐式联网；
  - 测试只用 `FakeLLM`/`httpx.MockTransport`，CI 不访问真实 LLM；
  - API key 只从 `PAPERSTEWARD_LLM_API_KEY` 读取并以 `SecretStr` 保存，不写日志/报告；
  - 非本机 endpoint 必须 HTTPS；不跟随重定向、不读代理环境；
  - 网关负责重试预算；provider 只做一次补全，错误只抛固定文案；
  - 富化缺失字段用 `None`/`()`/`OaStatus.UNKNOWN`/`confidence=0`，不伪造信号。
- 验收：
  - `pytest tests/unit/test_openai_compatible.py tests/unit/test_llm_factory.py tests/unit/test_enrich_contract.py tests/integration/test_cli_llm.py -q`
  - `papersteward llm ping --fake`（离线）与 `--live`（显式配置后手动）；
  - `make check`、`make test-integration`、`make migrate-check`、`make e2e`、`make demo`、
    `make secret-scan`。
- 范围外：富化持久化/迁移、embedding、R/摘要/分类/特殊桶/静态分、成本预算、流式输出、
  function calling、真实模型质量验收。
- 风险/回滚：真实调用可能产生费用和隐私外发；默认 fake + `--live` 授权 + 不提交 key。
  回滚删除 provider/配置/CLI 分支即可，无 schema 变更；P4 阶段门不依赖已删除的密钥。

### P4-S2：富化信号持久化与中性值

- 目标：把 `EnrichmentSignals` 持久化到独立表，保留 confidence/source/provenance；
- 输出：新增迁移、实体/仓储/映射、幂等 upsert、中性值回填；不调用真实 LLM；
- 验收：迁移 up/down、缺失字段中性值、来源可追溯、重复运行幂等；
- 范围外：R/摘要/分类；预估 1–2 天。

### P4-S3：相关度 R（embedding + rubric + 证据）

- 目标：按设计 §10.2 计算 R，证据缺失降级/转人工；先接离线/Fake embedding；
- 输出：`pipeline/relevance.py`、rubric prompt/schema、golden fixtures；
- 范围外：真实 embedding 供应商选择（需单独 ADR）；预估 2–3 天。

### P4-S4：100 字概括与三槽校验

- 目标：生成 ≤100 非空白字符摘要 + what/how/finding/limitation + evidence；
- 输出：summary prompt/schema 版本、长度/证据校验、失败一次修复、`needs_review`；
- 范围外：全文解析；预估 1–2 天。

### P4-S5：分类、置信度与边界案例

- 目标：主/副标签唯一性、置信度阈值、低置信度转人工；绑定已确认 taxonomy 版本；
- 输出：`pipeline/classify.py`、分类 golden、标签/桶写入服务；
- 范围外：特殊桶提名；预估 1–2 天。

### P4-S6：特殊桶提名与桥接假设

- 目标：review/distant/classic 三类提名、理由/证据与远邻 bridge memo；
- 输出：`pipeline/special.py`、提名 schema/测试；
- 范围外：配额/展示（P7）；预估 1–2 天。

### P4-S7：静态分 A/E/S 与快照

- 目标：权威/额外价值/静态分确定性计算、版本冻结、快照追加写；
- 输出：`scoring/static.py`、分项解释、golden；
- 范围外：动态分/排序（P5）；预估 1–2 天。

### P4-S8：审核队列与 `process` CLI e2e

- 目标：低置信度/特殊提名/边界案例可列出、通过、改标签；完整 process 离线 e2e；
- 输出：review API/CLI、真实 Uvicorn e2e、操作文档；
- 范围外：日报/推送（P6）；预估 1–2 天。

## P4-S1 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PAPERSTEWARD_LLM_PROVIDER` | `fake` | `fake` / `openai_compatible`（`openai` 为别名） |
| `PAPERSTEWARD_LLM_BASE_URL` | `https://api.openai.com/v1` | API 根地址；非本机必须 HTTPS |
| `PAPERSTEWARD_LLM_API_KEY` | 空 | 只放环境/`.env`；`SecretStr`，不打印 |
| `PAPERSTEWARD_LLM_MODEL` | 空 | `openai_compatible` 时必填 |
| `PAPERSTEWARD_LLM_TIMEOUT_S` | `30` | 单次请求硬超时（秒） |
| `PAPERSTEWARD_LLM_MAX_ATTEMPTS` | `3` | 网关 provider 重试上限 |
| `PAPERSTEWARD_LLM_PROMPT_COST_PER_1K_USD` | `0` | 仅用于成本估算 |
| `PAPERSTEWARD_LLM_COMPLETION_COST_PER_1K_USD` | `0` | 仅用于成本估算 |

配置示例、OpenAI/DeepSeek/Ollama 用法与故障排查见
[LLM provider 配置指南](../llm-provider.md)。

## P4-S1 验证与 handoff

### 验证证据（2026-10-22）

- `make check PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p4-s1-make-check'`：
  ruff format/check、strict mypy（**83 源文件**）通过；**753 passed / 4 e2e excluded**
  （P3 优化后基线 700，新增 53 项）。
- `make test-integration PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p4-s1-integration'`：
  **233 passed**（基线 228，新增 5 项 CLI live/fake 路由测试）。
- `make e2e PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p4-s1-e2e'`：**4 passed**。
- `TMPDIR=/dev/shm make migrate-check`：base → 0005 → base → 0005 通过（无新迁移）。
- `make demo DEMO_PORT=8056`：临时库迁移/seed、`/health`、`/papers` 正常。
- `make secret-scan`、`git diff --check` 通过；未使用真实厂商 key/网络。
- 手工 loopback 验证：本机临时 HTTP server 模拟 `/v1/chat/completions`，
  `PAPERSTEWARD_LLM_PROVIDER=openai_compatible` + `llm ping --live` 返回
  `provider=openai_compatible`，请求路径正确、Bearer header 正确、usage 7/3 与
  latency 正常；**未调用任何真实厂商 API**。

### 能做什么

- 通过 `.env`/环境变量配置 OpenAI-compatible provider；默认 fake，真实调用需
  `llm ping --live` 或后续服务显式 `live=True`；
- 使用 `FakeLLM`/`MockTransport` 做确定性测试；`llm ping --fake` 无网络；
- 通过 `EnrichmentSignals`/`EnrichmentProvider`/`NeutralEnricher` 传递中性富化信号；
- 网关记录 token/成本/延迟，认证失败等不可重试错误不消耗重试预算；
- 端点/模型/key/超时/重试/单价均可配置，错误信息不含密钥或原始响应体。

### 不能做什么

- 未持久化富化信号（P4-S2）；未实现 R/摘要/分类/特殊桶/静态分；
- 未实现流式输出、function calling、embedding、多 provider 路由、每日成本硬上限；
- 未进行真实模型质量/价格/限流验收；默认不自动调度真实 LLM；
- 不读取备注，不把论文文本当指令执行。

### P4-S2 输入与开工约束

- 输入：`EnrichmentSignals`/`NeutralEnricher`、`Settings.llm_*`、`LLMProvider`/`LLMGateway`；
- 新增 `enrichment` 表必须走 **0006 迁移**，保留 confidence/source/provenance；
- 缺失字段继续用 `None`/`()`/`OaStatus.UNKNOWN`/`confidence=0`；重复富化必须幂等；
- P4-S2 默认仍用 `NeutralEnricher`/fake，真实富化需显式授权，测试离线；
- 建议文件：`core/entities.py`、`storage/models.py`、`storage/repositories.py`、
  `migrations/versions/0006_*.py`、映射与集成测试；不要提前实现 R。

### 回滚

- 撤销 P4-S1 代码/配置/文档即可；无 schema/依赖/数据变更；head 仍为 `0005`；
- 已配置的真实 key 留在用户 `.env`/环境中，不由代码删除；撤销后默认回到 FakeLLM。

### 规模说明

P4-S1 超过“≤8 文件/≤400 行”指引：真实 provider（网络/认证/超时/成本/错误边界）、
配置校验、CLI 双模式、富化中性契约、ADR 与离线测试/文档共同构成本次竖切；
未实现 P4-S2 的持久化或任何 R/摘要/分类/评分逻辑。

## 与 P5 的边界

- P4 只产出可审阅条目、摘要/分类/静态分；P5 才计算动态分、排序与解释；
- 任何真实 LLM 调用都必须显式授权；备注默认外发仍需 P6/P8 的单独授权流程。
