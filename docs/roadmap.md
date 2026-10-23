# Roadmap：运行进度与移除人工审核

> 状态：代码与文档已实现，自动检查和独立浏览器验收通过；Docker 重建因主机权限未完成。
> 本文保留产品与实现决策，并在末尾记录验收结果。

## 目标

日常流程只保留：**同步 / 处理 → 生成日报 → 阅读并记录反馈**。

模型输出作为最佳猜测直接进入阅读队列；低置信度、摘要质量问题只作为运行摘要中的质量标记，不再要求用户先完成一轮审核。

## 产品决策

1. **删除人工审核关卡**：移除审核页面、导航入口、审核 API 与 `papersteward review` CLI；不再创建新的开放审核项。
2. **低置信度分类不再拦截**：主标签与归属桶照常写入，论文可进入队列；用户直接阅读后用 `done / snooze / skip / reject` 表达判断。
3. **摘要质量不再作为门槛**：有摘要文本即可进入队列，即使校验状态为 `needs_review`；完全没有摘要的论文排除，并在运行摘要中显示“跳过（无摘要）”。
4. **特殊桶自动决策**：有效且置信度不低于 0.8 的提名自动归入特殊桶；其余提名标记为拒绝，不阻塞主队列。
5. **历史数据保留**：`review_item` 表只作为历史审计保留；旧的 `needs_review` 论文在下一次同步/处理时自动转为 `classified`。
6. **一次只运行一个 UI 任务**：有任务运行时禁止再提交，页面显示当前任务与“完成后再开始新任务”，避免静默排队。

## 一、移除人工审核

### 处理管线

- `pipeline/process.py`：`process_paper` 不再创建审核项或进入 `needs_review`；质量原因记为 `quality_flags`，只进入分析记录与运行摘要。
- 新增 `paper_analysis` 表（迁移 `0017`）：保存 relevance / summary / classification / special 与质量标记，供队列解释使用，替代过去借审核 payload 存分析数据的做法。
- 新增 `reconcile_legacy_reviews`：把旧 `needs_review` 论文转为 `classified`，复制旧审核 payload 到分析表，关闭历史开放审核项。
- 特殊桶：`auto_decide_special` 在分类后自动接受高置信有效提名；接受失败或低置信时拒绝待审记录，论文留在主队列。
- `ProcessOutcome` 移除 `needs_review` / `review`，新增 `quality_flags` 与 `auto_shelved`。

### 队列与日报

- `scoring/queue.py` 删除 `open_review`、`pending_special_nomination`、`unapproved_low_confidence_classification`、`summary_not_passed`、`relabel_requires_static_rescore` 等审核门槛。
- 保留 `summary_unavailable`（完全无摘要）、分类/桶一致性、静态快照与相关度门槛。
- 队列解释从 `paper_analysis` 读取证据与 R 置信度。
- 日报 `pending_review` 固定为 0；删除“待确认 N 篇”文案，字段保留以兼容 API。

### 删除入口

- 删除 `api/review.py` 与 `/review` 路由。
- 删除 `/ui/review` 页面、导航“审核”和所有“处理审核”链接。
- 删除 `pipeline/review.py`；`ReviewRepository`、`review_item` 表与实体保留，仅用于历史数据。
- 删除 `papersteward review list/approve/relabel` 子命令，只保留 `process`。
- 保留可选的 `papersteward shelf` 命令；特殊桶是阅读/整理入口，不是每日审核关卡。

### 历史兼容

- 不删除历史审核数据；新流程不再产生开放审核项。
- 旧 `needs_review` 论文在下一次处理时自动迁移，不重跑分类，避免标签冲突。
- `PaperStatus.NEEDS_REVIEW`、审核状态与事件枚举保留以读取旧库。

## 二、运行进度与今日页

### 运行页

- 主标题唯一，不再出现“等待运行 · 运行中”：
  - 排队：**任务已排队，等待开始**，并链接到当前正在运行的任务。
  - 采集：**正在采集论文**，显示来源与 Agent 生成的检索式（报告内列出全部检索式及理由）。
  - 处理：**正在处理论文 5 / 9**，显示当前论文标题与剩余篇数。
  - 日报/草案：**正在生成今日日报 / 研究草案**。
  - 终态：**同步 / 处理完成、失败、已取消**，附结果摘要。
- 步骤条：同步为 `排队 → 采集 → 处理 → 完成`；日报/草案为 `排队 → 生成 → 完成`。
- 进度：处理阶段使用原生 `<progress>`，显示 `已处理 5 / 9 篇（56%）`；采集/生成阶段使用不确定进度。
- 时间：显示已用时/已等待与“最近更新 N 秒前”；超过 5 分钟无更新提示可能卡住。
- 失败：显示最后阶段与进度、可操作错误、相同参数重试；认证/配置错误链接设置页。
- 运行中每 2 秒自动刷新，终态停止。

### 今日页

- 顶部当前任务卡片：任务类型、状态、阶段、`5 / 9`、当前论文、已用时、进度条，每 2 秒刷新。
- 任务结束时用 `HX-Refresh` 整体刷新，恢复操作按钮并更新统计。
- 指标卡片：
  - **可读论文**：已通过筛选和评分、可进入今日日报的论文。
  - **待处理**：已采集、等待分类与评分。
  - **待排期**：已处理完成、等待生成日报/评分快照的论文。
  - 删除“待审核”。
- “下一步”只推荐一个动作：连接模型 → 等待当前任务 → 同步/处理 → 生成日报 → 阅读/同步新论文。
- 有任务运行时禁用同步与日报按钮，服务端同时做并发校验。

## 三、数据与统计

- `Run.stats_json` 增加：
  - `updated_at`
  - `collected`（采集数、新增/已存在、去重、过滤）
  - `processing_total`、`processed`、`current_index`、`current_paper_title`
- 同步完成摘要：采集数、新增/已存在、处理数、自动归类数、特殊桶数、跳过无摘要数、用时。
- 新增 `RunRepository.running_ui_runs()`，用于并发保护、当前任务卡片和排队说明。
- 同一时间只允许一个 running `ui.*` 任务；同 `run_id` 保持幂等。

## 四、文案与文档

- 日常顺序：**同步 / 处理 → 生成日报 → 阅读并记录反馈**。
- 帮助页增加“怎么看运行进度/三个数字是什么意思”。
- README、quickstart、FAQ、troubleshooting、privacy 删除审核步骤与“待审核”。
- `docs/advanced/process-review-cli.md` 改为 `process-cli.md`，只保留处理命令。
- priority-queue、delivery-cli、shelves 删除审核阻塞说明。
- CHANGELOG 记录本次移除审核与运行进度改造。

## 五、测试与验收

- boundary/低置信分类：无审核项、状态 `classified`、可进入队列。
- 摘要 `needs_review` 仍可进入队列；无摘要被排除并计数。
- 旧 `needs_review` 论文：reconcile 后 `classified`，历史审核项关闭。
- 特殊桶：高置信有效提名自动归入；低置信/无效提名拒绝且不阻塞队列。
- 队列不再因 open review / pending nomination 排除。
- 审核页面/API/CLI 不存在。
- 运行页：排队、处理中、成功、失败、旧 stats 回退。
- 今日页：当前任务卡片、指标解释、按钮禁用、结束刷新。
- 并发：已有 running 任务时新提交返回 409。
- `make check`、`make e2e`、Docker 重建和浏览器手工验证通过。

## 六、风险与回滚

- 自动接受低置信分类可能降低精度；用质量标记提示，用户可直接跳过/拒绝。
- 特殊桶自动归档可能移出主队列；只接受高置信有效提名，其他留在主队列。
- 摘要未通过校验仍可能进入队列；这是“直接读论文”策略的取舍。
- 历史审核数据只读；需要恢复旧流程时应恢复操作前数据库备份。
- 部署重启会把当前未完成运行标记失败；重启后重新点一次同步即可。

## 七、实施顺序

1. `paper_analysis` 迁移与仓储；处理、reconcile、特殊桶自动决策。
2. 队列/日报去审核门槛；删除 review API/UI/CLI 与导航。
3. 运行统计、并发保护、运行页/今日页与样式。
4. 测试、文档、CHANGELOG；重建 Docker 并手工验收。


## 八、实施与验收记录（2026-10-23）

- 已实现迁移 `0017`、`paper_analysis` 仓储、历史审核兼容、自动特殊桶决策；保留历史审核实体与审计数据。
- 已移除审核页面、导航、API、CLI 和队列审核门槛；日报兼容字段 `pending_review` 固定为 0。
- 已实现运行统计、两秒刷新、进度条、失败重试、今日任务卡片、三个指标解释，以及运行期间 UI 提交返回 409。
- `make check`：864 项通过；`make e2e`：3 项通过。测试临时数据库使用 `PYTEST=".venv/bin/pytest --basetemp=<内存临时目录>"`，避免本机磁盘同步造成启动超时，迁移和事务校验保持完整。
- `make migrate-check`：upgrade → downgrade → upgrade 通过，迁移与 ORM 一致性检查通过。
- 独立浏览器使用临时工作区和离线模型验证了采集/处理进度、按钮禁用、任务完成自动刷新、统计更新与成功摘要。
- 后续调整（2026-10-23）：任务冲突不再返回 409 错误页，而是 303 跳到正在运行的任务；新增「停止任务」（RUNNING → CANCELLED，取消在阶段边界生效，进行中的草案生成结果不落库）；首次设置收敛为 `/ui/setup` 单页面单向推进，移除运行页自链接与首次设置期间的队列入口。
- Docker 重建尚未完成：当前用户无权访问 `/var/run/docker.sock`，环境的 `no new privileges` 也禁止 sudo 提权。恢复 Docker 访问权限后执行 `./scripts/docker-up.sh -d`，再验证实际容器工作区。
