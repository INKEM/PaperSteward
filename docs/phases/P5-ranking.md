# P5 优先级排序、动态分与解释

> 状态：**S1–S6实现完成；本地阶段门与独立验收通过（见 [P5 独立验收报告](P5-acceptance.md)）；Docker/CI通过，PR #7已合并；P5 tag待发布流程。**
> 依据：开发计划 §3 P5；设计 §10；P4 enrichment handoff/返工复验。
> 不新增依赖；测试固定日期、FakeLLM、临时 SQLite；禁止真实来源/LLM。

## 执行卡

共同验收：每卡 `make check`、相关集成/契约测试、`make secret-scan`；S6 跑完整阶段门。
每卡代码、测试、文档同交。前卡本地提交后下一卡从该提交建立 stacked 分支；合并/发布另行处理。

| 卡片 | 目标 / 输入 → 输出 / 文件 | 契约、失败测试与演示 | 范围外 / 回滚 |
|---|---|---|---|
| S1 D/F/H | 固定日期、最早公开日、28天完成历史、目标占比 → `scoring/dynamic.py`、unit/contract tests | 半衰期180/120/540；无阅读H=50；未来日期age=0；积压正收益减半；非法占比拒绝；pytest test_dynamic_score | 无P/U/持久化；撤销代码 |
| S2 P/rank | S/D/U、配置、前日rank → `scoring/priority.py`、unit/contract tests、公式ADR | 分层加权、一位小数；EMA 0.7、单日±8、静态重算reset；R/date/A/title/id稳定排序；非法分数拒绝；pytest test_priority | 无U学习/队列；撤销代码 |
| S3 U | 截止最近周日的阅读评分 → `scoring/preference.py`、unit/contract tests | 标签+top3副标签、作者/venue、lambda收缩、全局先验、覆盖率弱信号；无评分50；不读取备注；pytest test_preference | 无反馈写API/文本信号/调度；撤销代码 |
| S4 配置 | 现有scoring文档 → 扩展配置与保存边界、unit/integration tests | 层内/层间和=1，R≥.5/S≥.6/A≤.2、有限数、半衰期映射、非法保存零写入 | 无权重预览治理(P7)；新字段有默认、回退代码 |
| S5 快照/解释/队列 | G1/G2/G4精确绑定、classified/static/summary/tags/review → `scoring/queue.py`、`scoring/contracts.py`、API/CLI、integration tests | 合格主题候选、G4、R门槛、特殊排除、不可变日快照/解释、幂等/历史/事务；queue/score explain/scores snapshot | 无日报/发送/阅读反馈/归档/特殊分档；快照存既有JSON，无迁移；回退代码保留数据 |
| S6 golden/e2e | S1–S5 → golden fixture、真实Uvicorn e2e、操作指南/阶段handoff | 完整门槛→process/review→snapshot→queue/explain，EMA/冷启动/特殊排除；check/integration/migrate/e2e/demo/secret | 不开始P6；撤销新增验收/文档 |

## 决策与边界

- P4 静态快照只读，不为每日排序重算；完整日快照复用 `score_snapshot`，rank与解释存 breakdown_json。
- 特殊桶按最终 FILING bucket 判断，主题桶上的特殊 MEMBERSHIP 徽章不排除；特殊桶没有P/rank。
- 反馈仅消费既有 Rating 与 paper.done 事件；P6 负责创建反馈和归档。校准标注不是阅读评分。
- G4 runtime 的 target_shares 已是主标签占比，优先使用；纯函数支持显式子方向映射，不能猜测映射。
- 只计算/读取当前合格候选，不发送；读取接口不产生快照或状态迁移。
- 设计示例的示例组件算术与P4冻结后分层算术存在中间舍入差异，S2用ADR记录确切算术，分别golden，不静默改全局默认。

## S1 完成（2026-10-22）

- F/H/D、显式占比映射、自然日窗口、未来/未知日期、中性冷启动、积压阻尼已实现。
- `make check`：842 passed，95源文件mypy；新增9项unit/contract；secret-scan/diff通过。
- SQLite在受限工作区fsync停滞，沿用P2–P4的 `/dev/shm` 临时测试库；不改变同步设置。
- 无迁移/依赖；纯函数演示：`pytest tests/unit/test_dynamic_score.py tests/contract/test_dynamic_contract.py`。
- 不计算P/U/队列；回滚撤销S1提交，无持久化变化。

## S2 完成（2026-10-22）

- 分层P、独立示例组件算术、EMA/±8限制/静态重算reset、R/date/A/title/id并列排序已实现。
- ADR0015记录中间舍入：组件示例75.1；未舍入S=78.75→P75.1，P4冻结S78.8→P75.2。
- `make check`：846 passed，96源文件mypy；新增4项unit/contract；secret-scan/diff通过。
- RankConfig冻结稳定参数；无迁移/依赖。暂未学习U/生成队列；回滚撤销S2代码。

## S3 完成（2026-10-22）

- U按更新日00:00排他截止，lambda亲和度收缩、总体置信度收缩、覆盖率<30%再弱化。
- 主标签+top3副标签、父标签/全局先验、作者/venue分项；null不作负分；不读取备注。
- `make check`：851 passed，97源文件mypy；新增5项unit/contract；secret-scan/diff通过。
- 无反馈写入/后台任务/迁移；S5保存结果与截止依据。回滚撤销S3，不修改Rating。

## S4 完成（2026-10-22）

- 重用并强化既有层内/层间权重与R/S/A护栏：保存/计算前重新字段校验，NaN/Inf及构造绕过拒绝。
- 配置新增按主标签半衰期、积压阻尼；默认字段省略，原有G4报告/hash的JSON保持逐值兼容。
- `make check`：858 passed，97源文件mypy；新增7项unit/integration；非法保存无配置/事件残留。
- 无迁移/依赖；未实现P7全局权重预览/激活；生产仍读G4精确选定配置，import不自动激活。
- 回滚代码即可；非默认扩展配置若已保存，回旧代码前先导出备份（旧schema不识别扩展）。

## S5 完成（2026-10-22）

- 完整日快照复用score_snapshot；rank/解释与舍入差额存breakdown_json，无迁移(head0008)。
- 只消费G1/G2/G4选定版本；合格主题候选原子推进scored→queued；特殊归属无P/rank，徽章不排除。
- API/CLI与操作指南见[priority-queue.md](../priority-queue.md)。读取不写库，允许暂停期间审阅；生成执行schedule门。
- `make check`：864 passed，101源文件mypy；新增6项unit/integration，G4/排除/幂等/CLI/
  原子失败/外层回滚/次日EMA/历史解释均通过；secret-scan/diff通过。
- P4无review的历史R证据未持久化：明确标记unavailable、R置信度0，保留摘要证据，不伪造。
- 规模超过8文件/400行：契约、可信上下文的只读选项、快照与状态事务、API/CLI、失败路径
  集成测试和指南共同构成S5完整竖切；未开始S6/P6，无新依赖。
- 回滚保留日快照/状态；完整撤销需恢复操作前备份。无推送/阅读写入/归档/特殊分档。

## S6 完成与阶段验收（2026-10-22）

- 新增priority_golden.json：§10.10组件75.1、P4冻结后分层75.2、EMA74.8、固定日期F/H/D、
  完整冷启动日快照。QueueItem/ScoreExplanation/RankConfig契约冻结。
- 真实Uvicorn + CLI链路扩展：G4前queue/snapshot拒绝→G1–G4→ingest→process→review
  →snapshot→queue/explain；同日幂等、R置信度/证据、无阅读H50/无评分U50均断言。
- 补齐摘要未通过/静态或标签版本漂移/未经批准低置信度/倒序/同日依据变更/暂停只读/
  归档历史不改写/特殊桶阅读计数/周评分修订/备注SQL不加载等验收。
- e2e发现并修正P4分类payload.primary_tag为对象的兼容读取；纯函数半衰期拒绝NaN/Inf。
  这些为S6验收发现的P5修复，没有扩展P6行为。

### 完整本地阶段门

| 检查 | 结果 |
|---|---|
| make check | **876 passed / 4 deselected**；ruff格式/lint、strict mypy **101**源文件通过 |
| make test-integration | **265 passed** |
| make e2e | **4 passed**（真实localhost Uvicorn/子进程CLI，包含完整P5链路） |
| make migrate-check | base→**0008**→base→0008，upgrade/downgrade/upgrade成功 |
| make demo DEMO_PORT=8075 | 独立临时库迁移/seed、health/papers正常，服务退出 |
| make secret-scan / git diff --check | 通过 |

所有数据库使用pytest tmp_path（basetemp=/dev/shm/p5-s6-*）或mktemp临时库，未碰var数据库。
可复制命令：

```bash
make check PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p5-s6-final-check'
make test-integration PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p5-s6-final-integration'
make e2e PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p5-s6-final-e2e'
TMPDIR=/dev/shm make migrate-check
# demo必须显式指定临时PAPERSTEWARD_DATABASE_URL，避免写用户库
make secret-scan
```

### 验收矩阵

| 计划要求 | 证据 |
|---|---|
| 75.1示例 | test_p5_golden；中间舍入边界ADR0015，不改P4冻结值 |
| 无评分/无阅读U/H中性 | unit preference/dynamic、snapshot golden、真实e2e |
| 缺失A/E中性 | 继承P4静态golden/已有回归；P5只读冻结结果 |
| 特殊桶排除P | priority_queue集成：特殊FILING无P/rank、MEMBERSHIP保留；special_reading仍计H |
| 非法配置拒绝保存 | ranking_config/save：NaN/Inf、构造绕过、权重护栏、零配置/事件残留 |
| 每项解释 | QueueItem.explanation含分项/权重/贡献/置信度/版本/证据、舍入差额与稳定参数 |
| 稳定排序与历史 | priority unit、snapshot golden、次日EMA、同日幂等/倒序拒绝、归档历史冻结 |

### 非阻断限制与未执行项

- 本轮未运行Docker/远端CI/独立验收；没有合并、推送、创建PR或打tag，不声称发布Alpha。
- 继承P1 F5（Starlette/httpx警告）、P1 F3失败usage回归债；不在本阶段顺手改依赖。
- 额外覆盖率测量：纯函数报告dynamic90%/preference98%/priority100%/config99%；组合测量
  中SQLite/Alembic路径触发SQLAlchemy C扩展 `InternalTraversal object is not callable`，
  默认与ctrace均复现。普通check/integration/e2e均通过；不将该不完整报告计为全scoring
  覆盖率证据，不为测量修改依赖/应用数据库设置。后续维护窗口排查工具兼容。
- P4部分历史条目的R原始证据未持久化；解释明确缺失与置信度0，现存摘要证据/静态
  breakdown保留。无新R证据持久化、人工静态重算/新配置激活流程（后续版本治理）。
- 静态分一位小数传播会使示例P为75.2，独立组件示例仍为75.1，详见ADR0015。

## P5 Handoff → P6

### 当前能做什么

- 离线确定性计算F/H/D/U/P/rank；按G4精确配置批量追加日快照、原子入队、分页读取队列、
  查看单篇当前/历史解释；没有评分/阅读时中性，不自动发送。
- 冻结契约：scoring/contracts.py 的QueueItem、ScoreExplanation，priority.py的RankConfig；
  API/CLI见priority-queue.md。日快照沿用score_snapshot，rank/稳定参数存breakdown_json。

### P6可直接消费的输入

- `create_snapshots(session, day, now=...)`、`read_queue(session, day, now=..., limit, offset)`；
  日期必须按已确认runtime时区生成；读为空不代表生成成功，应先明确生成并检查excluded。
- 队列按rank/R/date/A/title/id排序。P6编排在该合格候选集上做容量/多样性/冷却，不重算
  静态分、不扩大R门槛、不混入特殊桶。首次生成已推进queued；不要重复写状态事件。
- `explain_score(..., day=...)`可构造归档当日评分快照；P6应保存当时的日期/版本/完整解释，
  不以未来的latest替换历史。历史解释不追溯重排；摘要/标签/备注归档仍需P6独立冻结。
- 阅读完成须写paper.done和Rating：tags_at_read含主标签/副标签，null分不等于低分；
  每篇按周截止前最后评分修订聚合，done按主标签统计（含特殊桶）。作者/venue目前从
  Paper读取，P6提供阅读时快照后可增强；备注默认不读不外发。
- 发送与归档必须保留ensure_active；真实发送还须执行暂停/安静/渠道等完整runtime门禁。
  `bound_context(enforce_schedule=False)`只用于只读审阅，不可用于发送。

### 不能做什么与回滚

- 无日报渲染/推送、阅读操作API、归档/导出、定时任务、特殊桶分档/配额、自定义视图。
- 不因本地实现完成而自动认可真实用户摘要或启用真实网络；真实模型质量仍待独立评估。
- 无新迁移/依赖，head0008。代码回退保留快照/入队状态；完整撤销操作须停应用并恢复
  操作前备份。旧代码不认识非默认配置扩展时须先导出并恢复旧配置版本。
- S6超过8文件/400行指引：golden/契约冻结、失败与隐私集成验收、真实e2e、验收发现修复、
  README/AGENTS/计划/CHANGELOG与handoff构成收尾；无P6实现。

### 提交链与发布状态

S1 `5049cfe` → S2 `5376b57` → S3 `1a06993` → S4 `df78ea9` → S5 `7685a79` → S6本分支提交。
每张卡前一提交验证后才开始下一卡；PR #7 已合并到 `main`（`ed2409b`，树与 `c9854fd` 一致），
CI 通过；P5 tag 待发布流程。P6 契约与输入已就绪。

## 独立验收（2026-10-22）

P5-S1–S6 通过独立验收：本地阶段门、Python 3.14.7 全新 clone、Docker 容器与 CI 均通过，
未发现阻断缺陷；P6 可以开始。P5 tag 尚未创建。完整证据、边界探针与未闭合限制见
[P5 独立验收报告](P5-acceptance.md)。
