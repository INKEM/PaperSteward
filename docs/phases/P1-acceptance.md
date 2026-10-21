# P1 整体独立验收报告

> **验收对象**：P1 领域模型、版本化文档与 LLM 网关（P1-S1–S6）
> **验收 commit**：`db814d0`（`test(p1-s6): freeze contracts and close P1 acceptance`）
> **验收日期**：2026-10-21
> **验收方式**：不修改实现代码；独立运行阶段门、全新 clone、手工契约/边界检查、Docker/CI 静态证据复核
> **结论**：**有条件通过（Conditional Pass）**。P1 的本地功能与阶段门通过；但仍有 2 个代码/契约缺口、1 个低优先级成本审计缺口，以及 Docker/CI 发布证据未闭合。**不建议打 P1 tag**，P2 可以在明确遗留项后启动。
>
> **后续状态**：F1–F3 已返工（见 [P1 handoff](P1-domain-documents-llm.md) §11）；Docker
> build/run 已闭环（healthy、`/health`、seed 后 `/papers`），仅剩 CI 运行证据。

---

## 1. 结论摘要

| 项目 | 结果 |
|------|------|
| `make check` | **通过**：98 文件格式正确；54 源文件 mypy 无错误；**336 passed / 1 deselected** |
| `make test-integration` | **通过**：**101 passed** |
| `pytest -q`（含 e2e） | **通过**：**337 passed** |
| `make migrate-check` | **通过**：0001 → 0002 → 0001 → 0002 |
| `alembic check` | **通过**：`0002 (head)`，无待生成迁移 |
| `make e2e` | **通过**：1 passed（真实 Uvicorn） |
| `make demo` | **通过**：`/health` ok；`/papers` 返回 p001（`discovered`） |
| `make secret-scan` | **通过** |
| 覆盖率（core/documents/storage/scoring/schemas/llm） | **97%** |
| 全新 clone（默认 Python 3.14） | **通过**：bootstrap/check/integration/migrate/e2e/demo/secret 全绿 |
| 正常业务流程 | **通过**：profile/taxonomy/config + FakeLLM 分类/摘要 + 标签/桶/摘要落库 |
| P1-S1–S3 返工 F1–F5 | **抽查通过**：见第 4 节 |
| Docker Compose 配置 | **通过**：`docker compose config` 可解析 |
| Docker build/run | **未通过**：无 daemon 可用，`docker compose build` 失败；无容器运行证据 |
| GitHub Actions | **无运行证据**：Actions API 返回 `total_count: 0` |
| P1 tag | **未创建**（与 handoff 的“证据未闭合”一致） |

**总体判定**：P1-S1/S2/S3/S5/S6 的正常路径与本地门通过；P1-S4 存在“配置 service 可绕过校验”的缺口；S4 的 scoring schema 不能表示特殊桶快照。由于 Docker/CI 证据未闭合，**P1 整体不能视为完全关闭**。

---

## 2. 阶段门证据

```text
make check               → 336 passed, 1 deselected
make test-integration    → 101 passed
pytest -q                → 337 passed
make migrate-check       → upgrade/downgrade/upgrade OK
alembic check            → No new upgrade operations detected（0002 head）
make e2e                 → 1 passed
make demo DEMO_PORT=8035 → health ok；papers p001；API 自动停止
make secret-scan         → OK
coverage                 → core/documents/storage/scoring/schemas/llm 合计 97%
```

全新 clone 使用默认 `python3`（3.14.7）执行 `make bootstrap`，随后 check / integration / migrate / e2e / demo / secret 全部通过，`git status --short` 为空。

---

## 3. 正常业务流程复核

独立执行并核对：

- `profile validate/import/confirm`、`taxonomy validate/import/confirm`、`doc diff`、旧版本自动归档；
- `config validate/import`、重复版本拒绝、`config.updated` 事件；
- `schema show/validate`：classification/summary/scoring/event 正反例；
- `llm ping --fake` / `--json`；无 `--fake` 明确返回 1，不访问真实网络；
- `LLMGateway`：合法输出、非法→修复成功、非法→修复失败、provider 超时重试；
- `record_llm_usage` 成本累计；
- profile/taxonomy/config + FakeLLM 分类/摘要 + 标签/桶/摘要落库的 P1 端到端测试。

主流程均通过，未发现正常 CLI/服务路径的回归。

---

## 4. P1-S1–S3 返工复核（F1–F5）

| 项 | 独立复核结果 |
|----|--------------|
| F1 taxonomy 结构复检 | **已修复**：4 个主标签 draft 确认时抛 `InvariantViolation`，状态不写 |
| F2 malformed `## T06` | **已修复**：抛 `DocumentError`，field 为 `taxonomy.heading`，带行号 |
| F3 事件回填 | **已修复**：先写 `T+10s` 再写 `T` 被拒绝；backdated transition 被拒绝且 paper 状态保持 `deduped` |
| F4 文档 CLI 配置错误 | **已修复**：`PAPERSTEWARD_API_PORT=70000` 时输出 `PAPERSTEWARD_*` 字段提示，无 traceback |
| F5 标签/桶聚合 | **已修复**：首次 secondary 被拒绝；第 6 个 secondary 被拒绝；首次 membership 被拒绝；第二个 filing 被 DB 唯一约束拒绝 |

结论：原 P1-S1-S3 报告的 F1–F5 关闭条件已完成；[原验收报告](P1-S1-S3-acceptance.md) 作为历史记录保留。

---

## 5. 发现项

| ID | 严重度 | 发现 | 复现/证据 | 建议 |
|----|--------|------|-----------|------|
| **F1** | **中高 / 建议 P2 前修** | `save_scoring_config()` 不调用跨字段校验；可直接持久化一个 `static.relevance=0.40` 的非法配置。正常 `config import` 路径会校验，但公开 service 可绕过 | 构造非法 `ScoringConfig` → `save_scoring_config` 成功；随后 `read_scoring_config` 抛 `ConfigError: scoring.static.relevance 必须 ≥ 0.50` | 在 `save_scoring_config` 写入前调用 `validate_scoring_config(config)`；补“非法配置不落库/不写事件”测试；或把该函数降为内部 API |
| **F2** | **中 / 需在 P5 前决策** | `scoring` JSON schema 只覆盖主题桶：`priority` 必填、无 `tier`；特殊桶快照（tier、无 P）无法通过 schema | `validate_mapping("scoring", {..., "tier": "high"})` → `SchemaError schema.scoring.priority: Field required`；P1-S6 已冻结该 schema | 二选一：① 增加 `special_scoring` schema（tier，无 P）；② 把 `scoring` 改成条件模型（priority 或 tier 恰好一个）。同步更新 freeze 测试与 P5 契约 |
| **F3** | **低** | provider 重试耗尽时抛原始 `LLMProviderError`，不带 `usage`；失败调用的 calls/retries（以及潜在成本）无法审计 | `LLMTimeoutError has_usage=False`；成功/修复失败 `LLMValidationError` 则有 usage | 在 `LLMProviderError` 上附 `usage`，或网关包装为带 usage 的错误；补失败成本测试 |
| **F4** | **发布阻断** | Docker build/run 与 CI 运行证据未取得 | `docker compose config` 通过；`docker compose build` 退出 2（`.docker` 只读/daemon 不可用）；`docker info` permission denied；GitHub Actions API `total_count: 0` | 在有可用 Docker daemon 的环境执行 build + compose up + `/health`/`/papers`，或触发 CI 并保留 `docker` job 证据；证据齐全前不打 P1 tag |
| **F5** | **依赖债务** | `StarletteDeprecationWarning`（httpx → httpx2）仍存在 | `make check` 每次出现 | 依赖升级窗口处理，不阻塞 P1 功能 |
| **F6** | 信息 | P0 Docker/CI 证据仍未关闭；P0 F1（500 header）已修复 | 见 [P0 验收报告](P0-acceptance.md)，本次 500 复测通过 | 与 F4 一并处理 |

> F1 的正常 CLI 路径安全：`config import` 使用 `import_scoring_config` 先加载/校验。但 `save_scoring_config` 是导出的 service API，P2/P3 直接调用时会破坏“非法配置不进入业务表”的 S4 目标。
> F2 若是“scoring schema 仅主题桶”的有意设计，应在 S4/S6 handoff 明确写出，并把特殊桶 schema 作为 P5 的显式交付；否则 P1-S6 的 schema freeze 不完整。

---

## 6. 分阶段判定

| 小阶段 | 判定 | 说明 |
|--------|------|------|
| P1-S1 领域模型 | **通过** | 实体/枚举/状态机/不变量与覆盖率通过 |
| P1-S2 迁移/仓储 | **通过** | 0002 迁移、约束、事务、事件追加通过 |
| P1-S3 文档 | **通过** | F1–F5 返工复核通过；正常路径与错误路径通过 |
| P1-S4 配置/schema | **有条件通过** | 正常 CLI 通过；F1/F2 两个契约缺口 |
| P1-S5 LLM 网关 | **通过（低优先级审计缺口）** | FakeLLM/修复/重试/成本主路径通过；F3 |
| P1-S6 契约/验收 | **本地通过，发布证据未闭合** | freeze/e2e/阶段门通过；F4/F5 |
| **P1 整体** | **有条件通过** | 本地功能完成；F1/F2 + Docker/CI 证据未关闭 |

---

## 7. 关闭条件与建议

1. **F1**：`save_scoring_config()` 写入前执行 `validate_scoring_config()`；补非法配置不落库/不写事件测试。建议在 P2 开始前完成。
2. **F2**：决定特殊桶评分 schema：
   - 新增 `special_scoring`（tier、无 P），或
   - 将 `scoring` 改为 priority/tier 条件模型；
   - 同步更新 `test_p1_freeze` 与 P5 契约。建议在 P5 开始前完成。
3. **F3**：给 provider 失败错误附 usage（或至少记录 calls/retries），再做真实 provider 的成本预算。
4. **F4**：补 Docker build/run 与 CI 证据；完成前保持“P1 代码完成、阶段 tag 待补证据”。
5. **F5**：安排 httpx/Starlette 依赖升级，消除弃用警告。

P2 可以从当前 handoff 启动；但建议把 F1 作为 P2 的前置小修，P1 状态保持 **有条件通过**，不创建 P1 tag。

---

## 8. 验收执行记录

- 验收人：独立 coding agent（未修改实现代码）
- 验收命令：`make check`、`make test-integration`、`make migrate-check`、`alembic check`、`make e2e`、`make demo`、`make secret-scan`、`pytest -q`
- 额外检查：全新 clone（默认 Python 3.14）、P1-S1–S3 F1–F5 返工复核、P1-S4 配置/schema CLI、P1-S5 FakeLLM/修复/重试/成本、P1-S6 freeze/acceptance、Docker Compose 静态校验、GitHub Actions 运行记录
- 验收产物：本报告
