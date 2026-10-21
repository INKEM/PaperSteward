# P2 本地阶段验收报告

日期：2026-10-22；Python 3.12；head 0004；S6 基线 `72336df`。

结论：**P2-S1–S6 实现与本地阶段门通过，可交接 P3**。Docker daemon 当前不可访问，
CI 本轮未触发；未声称 Docker/CI 通过，未打 p2-done tag。

## 需求与证据

| 计划/设计要求 | 验证证据 |
|---|---|
| G1/G2 显式确认，拒绝跳步/陈旧/重复 | test_onboarding.py；test_api_onboarding_profile.py；test_api_onboarding_taxonomy.py；CLI/e2e |
| G2 5–8 主标签、≥90% 覆盖、≤50% 单桶、正例/边界 | test_onboarding_taxonomy.py 的边界/反例；test_incomplete_report_is_visible_and_blocks_g2 |
| 文档版本、可读差异、精确前序绑定 | 档案/分类法 API integration；CLI edit/review diff；漂移拒绝测试 |
| 种子幂等导入，不覆盖既有数据 | test_api_calibration.py 的 import/冲突/事务回滚 |
| 独立 15–20 篇校准，双方完整标注 | cohort/批次失败与盲标测试；真实 API/CLI e2e |
| Spearman/Top-5 golden，标签/阅读方式一致率 | test_calibration_metrics.py；平均并列秩、常数与乱序反例 |
| 能提示档案或 rubric 问题，明确接受已知偏差 | 反向分数/愿读差异 diagnostics；低指标 G3 书面接受 API 测试 |
| 参数与只展示日报；四类意见/版本协商 | test_dry_run.py 的 golden；test_api_dry_run.py 四类负面反馈与新版认可 |
| G4 最新日报/认可/版本绑定，原子配置/状态/事件 | stale/漂移/审计失败/外层 rollback 测试；G1–G4 e2e |
| G4 前 enqueue/push/archive 拒绝 | test_every_pre_g4_stage_blocks_all_execution_paths；真实 Uvicorn 分阶段仓储调用 |
| 最小完整初始化体验 | CLI integration；test_cli_g1_to_g4_and_stale_review 的实际 CLI 子进程 |
| 确认只用已审依据、不自动获取最新答案 | CLI 确认只有 POST；校验和/跨服务器/陈旧 revision/hash/缺 --yes 拒绝 |
| 不向真实模型/生产库/外部渠道发送 | 所有 fixture/fake/tmp_path；CLI 禁用代理/重定向；无真实发送器 |

## 完整阶段门

| 验收项 | 结果 |
|---|---|
| make check | ruff format/check + strict mypy（67 源文件）；567 passed，3 e2e 排除 |
| make test-integration | 209 passed（包含全部 API/仓储/CLI/迁移回归） |
| make migrate-check | base→0004→base→0004；模型/迁移一致性测试通过 |
| make e2e | 3 passed：walking skeleton、API G1–G4/门禁、CLI G1–G4 |
| make demo | 临时库迁移/seed/health/papers 正常，API 自动停止 |
| make secret-scan / git diff --check | 通过 |
| 操作指南与 handoff | docs/onboarding-cli.md；P2-onboarding.md 的 S6/P3 handoff |
| Docker Compose 配置 | docker compose config --quiet 通过 |
| Docker build/run | **未验证**：socket permission denied；sudo 被 no-new-privileges 禁止 |
| CI / tag | **本轮未触发 / 未打 tag**；P1 历史 CI 不能替代当前提交证据 |

SQLite 验证全部使用 `/dev/shm` 下 pytest tmp_path/临时库，以规避当前工作区 fsync
阻塞；没有更改同步设置或写 var/papersteward.db。可复制命令见 P2 阶段文档 S6。

## 限制与回滚

- 合成校准/日报仅流程验证，不代表真实研究方向或模型质量。真实用户必须独立标注、
  阅读全文/报告后明确确认，不自动复用 fixture 答案。
- 最小体验选择 CLI；没有网页、鉴权、重置/再协商、实际采集/摘要/排序评分或推送调度。
- ACTIVE 表示确认门开放，后续生产操作还须遵守已选参数、暂停/安静时段/隐私。
- 保留 P1 F5（httpx 弃用警告）、F3（失败 usage 回归断言）非阻断债务。
- S6 无依赖/迁移；整体 P2 新增 0003/0004。不要为撤销 S6 降级迁移；完整撤销初始化
  要先停止应用、恢复操作前备份。单纯回代码/删除快照不会撤销状态，旧枚举也不认识新事件。
- 没有本轮已知 P0/P1 阻断缺陷；环境中无法完成的 Docker/CI 证据明确留待复核。
