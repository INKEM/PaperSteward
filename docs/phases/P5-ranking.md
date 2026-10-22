# P5 优先级排序、动态分与解释

> 状态：执行中；按 S1→S2→S3→S4→S5→S6 顺序，本地 stacked 分支。
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
