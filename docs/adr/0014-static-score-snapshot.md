# ADR 0014：静态分使用独立快照表

- **状态**：接受
- **日期**：2026-10-22
- **阶段**：P4-S7

## 背景

P4-S7 需要冻结并追加静态分 R/A/E/S。P1 已创建每日完整 `score_snapshot`，但它同时
要求 F/H/U/D/P 与 `priority`/`tier` 二选一；P4 尚未计算动态分/优先级，直接写入会
伪造 P 或产生同日快照冲突，阻塞 P5。

## 决策

新增独立表 `static_score_snapshot`（迁移 `0007`）保存静态分及版本/解释：

- 主键 `(paper_id, score_date)`，只追加、不提供 update/delete；
- 保存 R/A/E/S、confidence、`config_version`/`profile_version`/`taxonomy_version`、
  `breakdown_json` 与输入哈希；
- 版本未变时拒绝重算；版本变化时只允许更晚日期追加；
- P5 从最新静态快照 + 动态分组合每日完整 `score_snapshot`，不修改本表历史。

## 替代方案

| 方案 | 不采用原因 |
|------|------------|
| 直接写 `score_snapshot` | 必须伪造 F/H/U/D/P 或同日追加冲突；违反 P5 契约 |
| 给 `score_snapshot` 增加 nullable 动态字段 | 改变 P1 冻结表/约束，迁移与回滚风险大 |
| 本阶段不持久化 | 不满足“静态分版本冻结/快照”验收，P5 无稳定输入 |
| 每日重算静态分 | 违反静态分冻结与稳定性设计 |

## 影响

- 新增迁移 `0007` 与静态快照行/映射/仓储；不改变 P1 `score_snapshot`；
- 无新依赖、无真实 LLM；测试使用固定输入与临时 SQLite；
- 回滚：`alembic downgrade 0006` 删除静态快照表；不触碰 paper/run/event；
- P5 必须把静态快照作为输入，不能把本表当作完整日快照展示。
