# P2 初始化协商与确认门

> 状态：P2-S1 完成（2026-10-21）；S2–S6 未开始。依据：大阶段开发计划 §3 P2、设计方案 §4、P1 handoff §10.7。

## 小阶段卡片

共同约定：不接真实来源/LLM，不实现 P3–P6 算法；无新增依赖；每卡 0.5–2 天。

| 卡片 | 目标、输入 → 输出 | 契约与测试 | 验收命令 | 范围外 |
|------|------------------|------------|----------|--------|
| S1 | P1 契约 → 初始化状态机、持久化、确认事件、统一门禁、start/status API | 0003 可逆迁移；顺序/重复/过期确认拒绝；G4 前入队/推送/归档拒绝 | `make check`、`make test-integration`、`make migrate-check` | 草案、门槛业务证据生成、确认 UI |
| S2 | S1 + profile fixture → 档案草案/导入/编辑/确认，接入 G1 | 复用 documents.service；确认绑定文档 ID/版本；非法档案不推进 | `make check`、profile/onboarding 集成测试 | 分类法/校准 |
| S3 | S2 + taxonomy/种子标注 → 分类法校验、确认、影响预览，接入 G2 | 5–8 主标签、覆盖率 ≥90%、单桶 ≤50%；边界失败用例 | `make check`、taxonomy/onboarding 集成测试 | 真实分类算法 |
| S4 | S3 + 合成种子 → 幂等导入、15–20 篇校准 harness，接入 G3 | Spearman/Top-5 golden、已知偏差书面接受；报告绑定前序版本 | `make check`、calibration 集成测试 | 真实来源/评分实现 |
| S5 | S4 + 固定参数/日报 fixture → 只展示试运行、参数确认，接入 G4 | 版本绑定、显式确认；真实 Uvicorn 门禁 e2e；不发送 | `make check`、`make e2e`、`make demo` | 正式推送/编排算法 |
| S6 | S1–S5 API → 最小 CLI/HTMX 流程与阶段验收 | 薄 UI；完整 G1–G4 路径；错误可操作 | 完整阶段门（含 secret-scan） | 高级交互、多用户 |

S2–S6 开工时补充实际文件与 fixture 清单；不得据本表提前实现。

## S1 冻结契约与范围

- `seeds → profile → taxonomy → calibration → dry_run → active`；种子可选，start
  直接进入 profile。无记录等同 seeds；GET 只读，start 幂等，ACTIVE 不可被 start 重置。
- G1/G2/G3/G4 分别允许 profile/taxonomy/calibration/dry_run 前进一步；禁止跳门、
  倒退、重复确认。每次确认携带当前 revision、用户标识、产物 ID/版本和变更摘要。
- S1 提供内部 `confirm_gate` 接口，**必须注入可信业务证据校验器**；外部请求不能
  上传 `passed=true` 或选择校验器。S2–S5 分别实现真实证据校验；S1 不提供通用
  HTTP 确认接口。测试中的成功路径使用明确标识的合成校验器，仅验证流程。
- 单用户 singleton 表 `onboarding` 与迁移 0003；revision 条件更新防止过期确认；
  状态和事件同事务写入。服务不 commit，由调用方 session_scope/显式事务提交。
- 追加 `onboarding.started`、`onboarding.g1.confirmed` 至 `onboarding.g4.confirmed`
  事件。事件保存确认者、产物 ID/版本、摘要与迁移两端状态/revision。
- `PaperRepository.add/transition` 的 queued/pushed/archived 入口与
  `ArchiveSnapshotRepository.add` 共用 ACTIVE 检查；无记录也拒绝。
  纯领域状态机仍只判断合法边，不隐式访问 DB；未来外部推送必须先调用同一门禁。
- API：`GET /onboarding` 返回 stage/revision/next_gate；`POST /onboarding/start`
  显式开始/幂等恢复。无种子导入、草案、校准、日报或推送端点。

## S1 验收与 handoff

### 能做什么

- 持久化/恢复初始化进度；开始幂等，不重复记事件；内部顺序确认记录用户和产物版本。
- 拒绝跳门、重复门、过期 revision、校验器拒绝；状态与事件同事务撤销。
- 所有现有仓储 queued/pushed/archived 写入入口受统一 G4 门禁约束，包括特殊桶
  shelved → pushed、稍后重入队及直接 add；原有文献状态机仍有效。
- start/status 可经 API 演示（先显式迁移并启动本机 API）：

```bash
.venv/bin/alembic upgrade head
.venv/bin/uvicorn papersteward.main:app --host 127.0.0.1 --port 8000
# 在另一个终端运行；第二次 start 返回相同进度
curl -fsS http://127.0.0.1:8000/onboarding
curl -fsS -X POST http://127.0.0.1:8000/onboarding/start
```

### 不能做什么与 S2 输入

- 目前无外部通用确认端点、初始化 CLI/页面、业务校验器、种子导入、校准或日报。
  测试中的 ACTIVE 是合成流程证据，不代表真实业务初始化已验收。
- S2 可直接复用 `core.onboarding.GateApproval`、`onboarding.service.confirm_gate`
  和 `storage.onboarding.get_onboarding/ensure_active`。S2 的可信 G1 校验器应检查
  确认产物 ID/版本、已确认 Profile 与内容完整性，再由明确用户确认命令调用。
  状态推进、产物确认和事件应置于同一 session_scope 中。
- G2/G3/G4 校验器依次检查覆盖率/均衡/边界、校准指标或书面接受、参数及试运行认可；
  必须绑定前序文档版本。不得从外部 JSON 构造“已通过”并直接推进。
- 目前没有正式发送服务；P6 的发送操作必须先调用 ensure_active。
- 重大方向变化再初始化/撤销激活不在 S1；不得以 start 重置已确认状态。

### 验证证据

2026-10-21 本地 Python 3.12：

- `make check`：ruff format/check、strict mypy 通过；**380 passed**，1 e2e deselected。
- `make test-integration`：**114 passed**（包含 API、0003 往返与模型一致性）。
- `make migrate-check`：base → 0003 → base → 0003 成功。
- `make e2e`：真实 Uvicorn walking skeleton **1 passed**。
- `make demo DEMO_PORT=8775`：临时库迁移/seed/health/papers 成功。
- `make secret-scan`、`git diff --check`：通过。
- 新增 41 个测试实例（29 单元 + 12 集成）；覆盖所有门前状态、确认失败、审计失败、
  外层事务回滚、跨 Session 恢复、ACTIVE 后允许操作和原有迁移约束。
- 测试发现并修复 SQLite legacy SAVEPOINT 在外层未实际 BEGIN 时提前提交的问题；
  局部原子块显式确保外层事务存在，不改变全局连接配置。
- 运行环境普通文件系统上 SQLite 同步写入挂起（最小 CREATE TABLE 可复现），因此
  上述 DB 验收使用 `/dev/shm` 临时文件库，未降低 synchronous 设置，未触碰 var 库。
  可复现命令如下；Docker/CI 未在本轮重新验证，不声称 P2 阶段整体验收。

```bash
make check PYTEST='.venv/bin/pytest --basetemp=/dev/shm/papersteward-p2-s1-check'
make test-integration PYTEST='.venv/bin/pytest --basetemp=/dev/shm/papersteward-p2-s1-integration'
TMPDIR=/dev/shm make migrate-check
TMPDIR=/dev/shm make e2e PYTEST='.venv/bin/pytest --basetemp=/dev/shm/papersteward-p2-s1-e2e'
```

### 风险、回滚与规模说明

- 迁移仅新增 onboarding；不回填旧库为 ACTIVE，不改变既有领域表内容。
- 停止应用后 `alembic downgrade 0002` 删除初始化状态；审计事件保留。重新 upgrade
  后门禁关闭。完整回滚旧版本代码须恢复 P2 前备份（旧枚举不认识新事件）。
- 本次超过 ≤8 文件/≤400 行指引：状态/迁移/仓储执行门禁/API 构成一个完整竖切，
  加上失败与回滚测试、P1 迁移 head 断言更新、ADR/阶段卡片/交接与状态文档。
  无新增依赖，未推进 S2–S6；P1 既有表/schema 不变，freeze 测试保留旧契约并允许
  迁移追加新表。
- P1 F5（Starlette/httpx 弃用警告）仍为非阻断依赖债务。
