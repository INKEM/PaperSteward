# process 操作

`process` 把 screened/enriched/summarized 文献推进到 `classified`，高置信有效特殊桶提名自动归入 `shelved`。只写本地 SQLite，不推送、不排序、不调用真实文献来源；`--live` 会访问配置的模型。

## 前置

- 已完成初始化确认且阶段为 active；
- 已运行 `alembic upgrade head`（当前 `0018`）；
- 离线用 `--llm-fixture`，真实 LLM 必须显式 `--live` 且配置 provider/model/key。

## 处理一篇

```bash
papersteward process --paper p_xxx \
  --llm-fixture tests/fixtures/process/e2e.json \
  --json
```

- `--paper` 缺省时处理最多 `--limit` 篇 screened/enriched/summarized；单一 `--llm-fixture` **只支持
  `--paper` 单篇**，批量请逐篇处理或使用 `--live`；
- fixture 是 JSON object，必须含四个键，按此顺序回放：
  `relevance`、`summary`、`classification`、`special_nomination`；
- 每个值可以是 object 或 JSON 字符串；classification/special schema 见
  `papersteward schema show classification|special_nomination`；
- 成功后写 enrichment、summary、`paper_tag`/`paper_bucket`、静态快照，与 `paper_analysis`；命令失败时同一事务回滚；
- 已有 enrichment 直接复用，不会被中性 provider 覆盖；仅缺失时才写中性信号；
- 缺摘要/摘要无法生成时跳过 summary，仍执行 R/分类/特殊桶/静态分，写入
  `summary:missing_abstract`（或 `summary:unavailable`）作为质量标记，不使批次失败；
- `--live` 在 provider=fake 时明确拒绝；LLM 配置/schema/网络错误只打印可操作信息，
  不输出 traceback。

退出码：

- `0`：全部处理完成（质量问题保留在 quality_flags）；
- `1`：未激活、schema/证据校验失败、版本冻结冲突、数据库错误；
- `2`：参数错误（argparse）。

## 静态 A/E 输入边界

process 从 enrichment 读取 `venue_level`、`code_url`、`data_url`、`oa_status`，
并结合 `published_at`；`author_scores`、`citation_percentile`、`reproducibility`、`rigor`
是 `process_paper` 的可选 Python API 参数，当前 CLI 不暴露。缺失项按 0.5 中性值并
在静态快照 `breakdown.authority/extra_value.reasons` 中显式标记，不会伪造精度。

## 数据与兼容

分析存入 `paper_analysis`，包括相关度、摘要、分类、特殊桶提名和 `quality_flags`。
`ProcessOutcome` 输出 `quality_flags`、`auto_shelved` 与最终状态。
有效且置信度不低于 0.8 的特殊桶提名自动接受，其余拒绝；接受失败会保留主题归属。
旧 `needs_review` 论文在下一次同步 / 处理时转为 `classified`，复制旧分析并关闭历史开放项，不重跑分类。
历史 `review_item`、状态与事件保留用于审计。人工审核 API、网页和 CLI 已移除。

## 边界

处理只写本地数据；不投递、不生成日报，不写完整的 F/H/U/D/P 日快照。
网页挖掘自动刷新队列；高级 CLI 可执行 `scores snapshot`，随后，符合相关度、分类与静态快照条件的论文进入队列。
有摘要文本即可进入队列，质量校验状态不阻塞；完全无摘要的论文排除。
回滚前停止服务并保存完整数据目录；完整恢复使用操作前备份。
