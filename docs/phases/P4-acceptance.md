# P4 独立验收报告

> **验收对象**：P4 富化、摘要、分类与静态评分（P4-S1–S8）  
> **验收 commit**：`239b732`（`feat(p4-s8): add offline processing and human review queue`）  
> **验收日期**：2026-10-22  
> **验收方式**：不修改实现代码；独立运行阶段门、全新 clone、Docker、CI、process/review 边界复现  
> **结论**：**未通过（Not Accepted）**。本地阶段门、Docker 与 CI 均通过，但 `process` 存在两个阻断性功能缺陷：覆盖既有富化信号造成数据丢失，以及无摘要文献使整批处理失败而不是转人工审核。P4 需返工后复验，P5 不应在修复前开始。

---

## 1. 结论摘要

| 项目 | 结果 |
|------|------|
| `make check` | **通过**：189 文件格式化；94 源文件 strict mypy；**827 passed / 4 e2e excluded** |
| `make test-integration` | **通过**：**248 passed** |
| `make e2e` | **通过**：**4 passed** |
| `make migrate-check` | **通过**：base → 0008 → base → 0008 |
| `make demo` | **通过**：临时库迁移、seed、`/health`、`/papers` |
| `make secret-scan` / `git diff --check` | **通过** |
| 全新 clone（默认 Python 3.14） | **通过**：check / integration / migrate / e2e / demo / secret |
| Docker build/run（当前代码） | **通过**：0008 head；UID 10001；healthy；doctor、`/health`、`/papers`、`/onboarding`、`/review` 正常 |
| CI | **通过**：run [39736075312](https://github.com/INKEM/PaperSteward/actions/runs/39736075312)（`239b732`），quality 3.11/3.12 + docker smoke 全绿 |
| P4 tag | **未创建**（存在阻断缺陷） |

**总体判定**：工程门与发布证据齐全，但 **P4-S8 的 process 竖切不满足其自身文档的“保留既有富化/缺摘要转人工”契约**；因此 P4 不能关闭。

---

## 2. 阶段门与发布证据

### 2.1 当前工作区

```text
make check PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p4-accept-check'
  → 827 passed, 4 deselected；ruff/mypy 通过

make test-integration PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p4-accept-int'
  → 248 passed

TMPDIR=/dev/shm make migrate-check
  → base → 0008 → base → 0008

make e2e PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p4-accept-e2e'
  → 4 passed

make demo DEMO_PORT=8067
  → /health ok；/papers 返回 p001

make secret-scan
  → OK
```

### 2.2 全新 clone

默认 Python 3.14.7，`make bootstrap` 后 check/integration/migrate/e2e/demo/secret 全部通过；`git status --short` 为空。

### 2.3 Docker / CI

- 当前代码 `docker compose build` 成功；容器 UID 10001、0008 head、healthy；
- `papersteward doctor` 全部 OK；`/health`、`/papers`、`/onboarding`、`/review` 均正常；
- GitHub Actions run [39736075312](https://github.com/INKEM/PaperSteward/actions/runs/39736075312)
  head `239b732`：`quality (py3.11)`、`quality (py3.12)`、`docker compose smoke` 三个 job 全部 success。

**发布证据本身不是失败原因**；失败在 process 的功能契约。

---

## 3. 阻断缺陷

### F1（高，阻断）：process 覆盖既有 enrichment，造成数据丢失

`pipeline/process.py::_static_snapshot` 每次无条件调用：

```python
enrichment, _ = persist_enrichment(session, paper, now=lambda: now)
```

`persist_enrichment` 默认使用 `NeutralEnricher`，而 `EnrichmentRepository.save` 对已有行执行更新。因此已有非中性富化信号会被中性值覆盖。

独立复现：

```text
预置 EnrichmentSignals:
  venue_level="a*", citation_count=120,
  code_url/data_url=<https>, oa_status=open,
  source="real-fixture", confidence=0.9

执行 process_paper() 后:
  enrichment.source = "neutral"
  venue_level = None
  citation_count = None
  code_url = None
  static_A = 50.0
  static_E = 55.0
```

影响：

- P4-S2 已持久化的来源/venue/引用/code/data 被静默丢弃；
- P4-S7 静态 A/E 无法使用真实富化信号，pipeline 的 A 基本固定在中性值；
- 与 P4-S2“幂等保存并提供给 P4-S3+ 使用”、P4-S8“enrichment → … → static”契约冲突。

**修复方向**：`_static_snapshot` 应先 `EnrichmentRepository.get(paper.id)`；已有行直接使用，仅在不存在时才写入中性富化；不得用默认 provider 覆盖既有来源。

### F2（高，阻断）：无摘要文献使 process 整批失败，未转人工审核

P4 文档明确：

> 缺少摘要时在调用 LLM 前拒绝，**由调用方转人工**。

但 `process_paper` 直接调用 `generate_summary`，后者对 `abstract=None` 抛：

```text
SummaryError: summary.abstract: 缺少摘要，不能生成概括
```

`process_paper` 没有捕获该错误，也没有打开 `review_item` 或把论文置为 `needs_review`；CLI 批次因此整体失败，其他可处理文献也回滚。

独立复现：

```text
screened paper, abstract=None
→ process_paper() raises SummaryError
→ 无 review_item，状态仍 screened
```

P3 的来源适配器允许缺摘要（OpenAlex/Semantic Scholar 均缺字段降级），所以这不是纯合成边界，而是可进入 screened 的真实数据形态。

**修复方向**：在 process 编排中捕获“无摘要/无法生成摘要”的业务错误，写入可读原因（如 `summary:missing_abstract`），打开审核项并迁移到 `needs_review`；不要把整批回滚。若设计选择“无摘要不得处理”，则必须同步修正 P4-S4/S8 文档和 CLI 行为，并明确不是转人工。

---

## 4. 其他发现（非阻断或需确认）

### F3（中）：process CLI 未捕获 LLMError，真实 provider 失败会打印 traceback

`cli/processing.py::run` 只捕获 `AppError`、`DomainError`、`SQLAlchemyError`、`ValidationError`、`OSError`；`LLMValidationError`、`LLMProviderError`、`LLMConfigurationError` 都继承 `LLMError`，不在捕获范围。真实 LLM schema/网络/配置失败会向上冒泡为 traceback，违反“失败返回非 0 并给出可操作提示”。

另外 `process --live` 在 `PAPERSTEWARD_LLM_PROVIDER=fake` 时会由 `build_llm_provider` 直接返回 `FakeLLM`（factory 的 fake 分支先于 live 检查），不会像 `llm ping --live` 那样明确拒绝；用户可能以为在跑真实模型。

### F4（中/低）：process 的静态 A/E 没有使用调用方提供的 author/citation 等输入

`_static_snapshot` 只向 `compute_authority` 传 `venue_level/published_at/as_of`，没有传 `author_scores` 或 `citation_percentile`；`compute_extra_value` 只传 code/data/OA，未传 reproducibility/rigor。unit 测试验证了完整公式，但 process 路径的 A/E 基本退化为中性值。若这是 P4-S8 的有意限制，应在文档和 CLI 输出中明确“process 仅使用 enrichment 可得字段”；否则应补充输入/读取路径。

### F5（低）：batch + 单一 LLM fixture 只能处理第一篇文章

`process --llm-fixture` 创建一个按 relevance/summary/classification/special 顺序回放的 FakeLLM；默认 `--limit 10`。第二篇 screened 文献会复用最后一个响应并触发 schema 错误。单篇 `--paper` 可用，但批量离线处理没有明确限制/提示，也没有逐篇 fixture。

以上 F3–F5 不是 P4 关闭的首要阻断，但建议随 F1/F2 返工一并处理。

---

## 5. 已通过的范围

- P4-S1 真实 LLM 配置/provider/显式 live 门；测试仅用 FakeLLM/MockTransport；密钥不出现在错误/日志。
- P4-S2 enrichment 迁移/仓储/中性值/幂等写入本身通过。
- P4-S3 R、P4-S4 摘要、P4-S5 分类、P4-S6 特殊桶提名、P4-S7 静态分的单元/契约路径通过。
- P4-S8 审核队列 API/CLI、批准/改标签、G4 后的 process e2e、真实 Uvicorn 链路通过。
- 没有发现 G4 前入队/推送/归档被绕过；P4 不写动态分/priority/完整 score_snapshot。

---

## 6. 继承债务

- P1 F5：`StarletteDeprecationWarning`（httpx → httpx2）仍存在，非阻断。
- P1 F3：provider 失败 usage 的独立回归断言仍待补。
- P2/P3 阶段 tag 仍待发布流程；P4 本身也未打 tag。

---

## 7. 返工验收条件

P4 返工后至少满足：

1. 已有 enrichment 不被 process 覆盖；静态 A/E 使用已有来源数据（补回归：预置非中性 enrichment → process → 值保留）。
2. 无摘要/无法摘要的 screened 文献进入 `needs_review` 和 review 队列，不使整批回滚；补 CLI/服务测试。
3. `process` CLI 捕获 `LLMError` 并输出可操作错误；`--live` 在 provider=fake 时明确拒绝。
4. 明确 process 的静态 A/E 输入边界；若保留中性限制，写入 P4-S8/P5 交接。
5. 重新运行 `make check`、`make test-integration`、`make migrate-check`、`make e2e`、`make demo`、`make secret-scan`，以及 Docker/CI。

---

## 8. 最终判定

**P4 本地阶段门、全新 clone、Docker 与 CI 通过，但 process 竖切存在阻断性数据/流程缺陷，P4 验收不通过。**  
P4 保持未关闭、未打 tag；P5 不应在 F1/F2 修复并复验前开始。

---

## 9. 验收执行记录

- 验收人：独立 coding agent（未修改实现代码）
- 验收命令：`make check`、`make test-integration`、`make migrate-check`、`make e2e`、`make demo`、`make secret-scan`、`docker compose config/build/up`
- 额外检查：全新 clone（Python 3.14）、Docker 当前镜像、GitHub Actions run/jobs、预置 enrichment 的 process 复现、无摘要 process 复现
- 验收产物：本报告

## 10. 返工记录（实现方补充，非复验结论）

针对 F1–F5 的返工记录见 [P4-acceptance-rework.md](P4-acceptance-rework.md)。
本报告第 1–9 节仍是对 `239b732` 的独立验收结论；返工提交待独立复验，P4 仍未关闭，
P5 不得在复验通过前开始。
