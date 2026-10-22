# P4 验收返工记录

> **返工对象**：`docs/phases/P4-acceptance.md` 中 F1–F5
> **原验收 commit**：`239b732`
> **返工分支**：`fix/p4-acceptance-rework`
> **状态**：返工完成，**独立复验通过**；P4 可以关闭，P5 可开始。
> **日期**：2026-10-22

## 1. 修复对照

| 编号 | 原问题 | 修复 | 回归 |
|---|---|---|---|
| F1（阻断） | process 无条件用 NeutralEnricher 覆盖既有 enrichment | `_static_snapshot` 先读 `EnrichmentRepository.get`；已有行直接复用，仅缺失时写中性信号 | 预置 `a*`/citation 120/code/data/open/`real-fixture` → process → 原 signals 完全保留；A=70、E=80 |
| F2（阻断） | 缺摘要抛 `SummaryError`，整批回滚 | 缺摘要或 `SummaryError` 时 summary=None，记 `summary:missing_abstract`/`summary:unavailable`，继续 R/分类/特殊/静态并进入 review | 无摘要 paper → `needs_review` + open `review_item`，`payload.summary=null`，无异常 |
| F3（中） | CLI 未捕获 `LLMError`；`--live` 在 provider=fake 时静默用 FakeLLM | `run` 捕获 `LLMError` 并打印可操作信息；`--live` + fake 明确拒绝；关闭真实 provider | 三条 CLI 测试：fake+live、LLMProviderError、无 traceback |
| F4（中/低） | process 的 A/E 只使用部分 enrichment 字段 | 明确输入边界：venue/code/data/OA/published 来自 enrichment；author_scores/citation_percentile/reproducibility/rigor 为 `process_paper` 可选参数；CLI 人读输出显示 R/A/E/S；缺失项在 breakdown reasons 标记 | 可选输入使 A=72、E=75；文档记录 CLI 限制 |
| F5（低） | 单一 fixture + batch 只正确处理第一篇 | 单一 `--llm-fixture` 仅允许 `--paper`；批量必须逐篇或 `--live`，错误信息明确 | CLI 测试拒绝无 `--paper` 的 fixture batch |

## 2. 代码与测试

- `pipeline/process.py`：enrichment 复用、summary 降级/审核、可选 A/E 输入、静态快照复用不变。
- `cli/processing.py`：LLMError、live/fake 门、fixture batch 限制、A/E/S 人读输出。
- 回归测试：
  - `tests/integration/test_process_review.py`：F1/F2/F4；
  - `tests/integration/test_cli_processing.py`：F3/F5；
  - 原有 process/review API/e2e 保持通过。
- 文档：`docs/process-review-cli.md` 增加 enrichment 保留、缺摘要转 review、A/E 输入边界、fixture batch 限制。

## 3. 返工后阶段门

```text
make check            → 833 passed / 4 e2e excluded；190 文件格式化；94 源文件 mypy
make test-integration → 254 passed
make migrate-check    → base → 0008 → base → 0008
make e2e              → 4 passed（含 process→review→approve）
make demo DEMO_PORT=8065 → /health、/papers 正常
make secret-scan      → OK
```

Docker（本地重建当前代码）：

```text
docker compose build  → 成功
docker compose up     → healthy；UID 10001；head 0008
papersteward doctor   → 全部 OK
/health /papers /review → 200
```

## 4. 复验前状态（历史）

- **独立复验**：已完成，见 §6；
- **CI**：返工已 push，run 39742936389（`f048a82`）与 main run 39743222329（`453560c`）均全绿；
- P4 tag 仍未创建；P5 在复验通过后可以开始；
- 继承 P1 F5（Starlette/httpx 警告）与 P1 F3 债务不变。

## 5. 复验建议

1. 预置非中性 enrichment 后执行 `process_paper`，断言 signals/source 未变且 A/E 使用该值；
2. 对 `abstract=None` 的 screened paper 执行 process，断言 `needs_review`、review open、无异常；
3. `process --live` 在 `PAPERSTEWARD_LLM_PROVIDER=fake` 下返回 1 且无 traceback；
4. `process --llm-fixture` 不带 `--paper` 返回明确错误；
5. 重跑本记录第 3 节全部命令与 Docker/CI。

## 6. 独立复验结论（2026-10-22）

> 复验 commit：`f048a82`（已合并 main `453560c`）；独立执行，不修改实现代码。

**结论：通过（Accepted）。** F1–F5 均闭环，P4 可以关闭；未发现新的阻断缺陷。

### 阶段门

```text
make check            → 833 passed / 4 e2e excluded；190 文件；94 源文件 mypy
make test-integration → 254 passed
make migrate-check    → base → 0008 → base → 0008
make e2e              → 4 passed
make demo DEMO_PORT=8069 → /health、/papers 正常
make secret-scan      → OK
```

全新 clone（默认 Python 3.14.7）：check / integration / migrate / e2e / demo / secret 全部通过。

### 关键回归复现

- **F1**：预置 `a*` / citation 120 / code+data / open / `real-fixture` enrichment，执行 process 后
  `enrichment.source` 仍为 `real-fixture`，venue/citation/code 未变；静态 A=70、E=80。
- **F2**：`abstract=None` 的 screened paper → `needs_review`、review open、
  `payload.summary=null`、无异常；不再整批回滚。
- **F3**：`process --live` + provider=fake 返回 1 且无 traceback；LLMProviderError 被捕获。
- **F4**：`process_paper(author_scores=(1.0,0.5), citation_percentile=0.8, reproducibility=1.0, rigor=1.0)`
  得到 A=72、E=75；CLI 人读输出包含 R/A/E/S。
- **F5**：`process --llm-fixture` 不带 `--paper` 明确拒绝；单 fixture batch 不再静默错放。
- 定向回归 6 项全部通过。

### Docker / CI

- 当前 `f048a82` Docker 重建成功；UID 10001、0008 head、healthy；doctor、`/health`、`/papers`、`/review` 正常。
- CI run [39742936389](https://github.com/INKEM/PaperSteward/actions/runs/39742936389)（`f048a82`）与
  main run [39743222329](https://github.com/INKEM/PaperSteward/actions/runs/39743222329)（`453560c`）
  的 quality 3.11/3.12 + docker smoke 全部 success。

### 最终判定

**P4-S1–S8 返工复验通过，P4 可以关闭。** 阶段 tag 仍留待发布流程；P5 可以开始。
P1 F5/F3 债务继续记录。
