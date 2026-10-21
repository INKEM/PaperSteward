# P1-S1–S3 独立验收报告

> **验收对象**：P1-S1 领域模型 / P1-S2 正式 schema 与仓储 / P1-S3 文档解析与版本确认  
> **验收 commit**：`8e91026`（`feat(p1-s3): add document parsing, diff, and version confirmation`）  
> **验收日期**：2026-10-21  
> **验收方式**：不修改实现代码；独立运行阶段门、全新 clone、手工契约检查、边界用例与静态审查  
> **结论**：**有条件通过（Conditional Pass）**。本地阶段门与正常业务流程通过；发现 4 个契约边界缺陷（另有 1 个需决策的聚合校验风险），其中“确认分类法未复检 5–9 主标签结构”建议在 P1-S4 前修复。

---

## 1. 结论摘要

| 项目 | 结果 |
|------|------|
| `make check` | **通过**：77 文件格式正确；43 源文件 mypy 无错误；**256 passed / 1 deselected** |
| `make test-integration` | **通过**：**73 passed** |
| `pytest -q`（含 e2e） | **通过**：**257 passed** |
| `make migrate-check` | **通过**：0001 → 0002 → 0001 → 0002 |
| `alembic check` | **通过**：无待生成迁移 |
| `make e2e` | **通过**：1 passed（真实 Uvicorn） |
| `make demo` | **通过**：`/health` ok；`/papers` 返回 `p001`，状态 `discovered` |
| `make secret-scan` | **通过** |
| 覆盖率 | 合计 **98%**；`documents/` **96%**；`storage/` **97%** |
| 全新 clone（默认 Python 3.14） | **通过**：bootstrap / check / integration / migrate / e2e / demo / secret 全绿 |
| 正常文档 CLI 流程 | **通过**：validate → import → confirm → 旧版归档 → diff → archive |
| 手工 DB 约束 | **通过**：主标签/filing/确认版本/事件 ID 等重复写入被拒绝 |
| P1-S4–S6 | **未验收**：SQL 配置校验、JSON schema、LLM 网关尚未实现 |

**总体判定**：P1-S1 与 P1-S2 的正常路径通过；P1-S3 主流程通过，但存在若干“绕过 parser 或错误输入”的契约缺口。建议按第 5 节修复后再把 P1-S1–S3 视为完全关闭；当前不打 P1 阶段 tag（P1-S4–S6 本来就未完成）。

---

## 2. 阶段门证据

```text
make check               → 256 passed, 1 deselected
make test-integration    → 73 passed
pytest -q                → 257 passed
make migrate-check       → upgrade/downgrade/upgrade OK
alembic check            → No new upgrade operations detected
make e2e                 → 1 passed
make demo                → health=ok；papers p001；status=discovered
make secret-scan         → OK
```

覆盖率独立复测：

```text
core + documents + storage 合计 → 98%
documents/                      → 96%
storage/                        → 97%
```

与 handoff 的差异：handoff 记 `documents 97% / storage 96%`；本次独立测量为 `96% / 97%`，属于覆盖率口径/取整差异，不影响验收结论。

---

## 3. 全新 clone 验收

在独立临时 clone 中执行默认 `make bootstrap`（Python 3.14.7），随后：

```text
make check               → 256 passed, 1 deselected
make test-integration    → 73 passed
make migrate-check       → upgrade/downgrade/upgrade OK
make e2e                 → 1 passed
make demo DEMO_PORT=8031 → health ok；papers p001（discovered）
make secret-scan         → OK
git status --short       → 空
```

说明 P1 新增的 PyYAML 运行时依赖没有破坏 README 的默认 bootstrap 路径；Python 3.12 的当前工作区同样通过全部本地阶段门。

---

## 4. 手工契约检查

### 4.1 P1-S1 领域层

独立检查覆盖：

- 实体不可变、枚举值稳定、UTC 时区规范化；
- `Summary` 100 个非空白字符上限；`PASSED` 摘要必须有溯源和证据；
- `PaperTag` Agent 标签必须有溯源；`PaperTag` 置信度 0–1；
- `Taxonomy` 标签代码唯一、已确认分类法不能为空标签；
- `ScoreSnapshot` priority/tier 恰好一个；特殊桶档位与主题桶 P 互斥；
- `Run` 终态必须有结束时间，且不早于开始时间；
- 状态机白名单：`done → archived` 唯一入口、`archived` 终态、`needs_review` 不得直达队列、`shelved → scored` 允许但必须先重算；
- 不变量：主标签/归属桶唯一、副标签上限、分类法 5–9 主标签、特殊桶优先级、R≥55 入队、摘要未通过不得推送、静态分冻结、快照追加、事件 ID/时间、归档不可变。

正常路径与单元测试一致；未发现实体/状态机层面的直接反例。

### 4.2 P1-S2 迁移、约束与仓储

- 0002 建立 14 张正式表；`paper` / `run` 替换 P0 stub；
- P0 `papers`/`runs` 数据升级：`new → discovered`、`ready → screened`、`completed → succeeded`、`error → errors_json`；
- 降级恢复空 P0 stub，正式表数据丢弃（handoff 已明确）；
- 手工验证 DB 约束：
  - 同一文献第二条 primary tag → `IntegrityError`；
  - 同一文献第二条 filing bucket → `IntegrityError`；
  - 同一 `(paper_id, score_date)` 第二条快照 → 领域不变量拒绝（DB 主键兜底）；
  - 重复 event ID → `IntegrityError`；
  - 同一 profile 两个 confirmed 版本 → `IntegrityError`；
  - 终态 Run 无 `finished_at` → 领域校验拒绝；
- 仓储只 `flush` 不 `commit`；回滚后数据不落库；
- 事件、评分快照、归档快照仓储没有 update/delete 方法；
- JSON/UTC 映射往返正常；
- 手工 `alembic check` 无差异。

### 4.3 P1-S3 文档解析、diff、服务与 CLI

- `profile validate` / `taxonomy validate` 对合法 fixture 返回 0；
- 非法 profile 返回 1，错误包含文件、行号与 `front_matter.core_questions`；
- profile：3–5 个核心问题、子方向 `target_share` 和为 1、`deep_skim_ratio` 和为 1、重复 ID 拒绝、未知字段保留；
- taxonomy：5–9 主标签、每节定义/纳入/排除/正例必填、禁止“其他”兜底、副标签父级存在、重复代码拒绝；
- diff：基于实体而非 Markdown；keyed list 按 code/id；标量列表忽略顺序；无差异输出“无差异”；
- 服务：import 为 draft；confirm 归档旧版并追加事件；重复确认被状态机拒绝；draft 归档被拒绝；解析失败不写库；
- CLI：import/confirm/archive/diff 正常流程通过；`--json` 输出可解析；非法文件路径/缺失版本返回 1。

---

## 5. 发现项

| ID | 严重度 | 发现 | 复现/证据 | 建议 |
|----|--------|------|-----------|------|
| **F1** | **高 / 应在 S4 前修** | `confirm_taxonomy()` 不复检 `ensure_taxonomy_shape`；通过仓储构造的 4 主标签 draft 可以被确认 | 构造 4 个 primary `Tag` → `TaxonomyRepository.add` → `confirm_taxonomy` 返回 `CONFIRMED confirmed 4` | 确认前调用 `ensure_taxonomy_shape(taxonomy)`，并补“4/10 主标签不可确认”测试 |
| **F2** | **中** | 分类法正文中格式错误的 `## T06`（缺少名称）被静默忽略，不报错 | 5 个合法小节 + `## T06` → `papersteward taxonomy validate` 返回 0，解析结果只有 T01–T05 | 遇到 `##` 标题但不符合 `## Txx 名称` 时抛 `DocumentError`，并定位行号；补 CLI/单元测试 |
| **F3** | **中** | `EventRepository.append()` 与 `PaperRepository.transition()` 不校验事件时间非递减，可写入早于最后一条的事件 | 先 append `T+10s`，再 append `T` → 成功；正常 transition 也能回填 `T` | 在 append/transition 路径调用 `ensure_event_appended` 或查询最后事件时间；同步修正当前“先 T+1 再 T”的测试语义 |
| **F4** | **低** | 文档 CLI 的数据库子命令（import/confirm/archive/DB diff）在配置非法时打印完整 traceback，而不是字段级可操作提示 | `PAPERSTEWARD_API_PORT=70000 papersteward profile import ...` 退出 1，stderr 为 pydantic traceback | 在 `_session()`/`run_import/confirm/archive/diff` 捕获 `pydantic.ValidationError`，复用 `format_settings_error()`；补 CLI 失败路径测试 |
| **F5** | **中 / 需决策** | 仓储层未调用聚合不变量：0 个主标签、7 个副标签都能写入；DB 只能保证“最多一个”主标签/filing | 无标签文献查询为 `[]`；连续写 7 个 secondary 成功 | 若 P3/P4 会有 `set_classification` 聚合服务，可记录为 deferred；否则在仓储增加聚合写入 API，调用 `ensure_single_primary_tag` / `ensure_secondary_tag_limit` |
| **F6** | 信息 | 仍有 `StarletteDeprecationWarning`（httpx → httpx2） | `make check` 每次出现 | 作为依赖债务处理，不阻塞 P1-S1–S3 |
| **F7** | 继承风险 | P0 的 Docker/CI 运行时证据仍未关闭；本地无 Docker。P0 F1（500 缺失 `X-Request-ID` 头）已由 `fa7d931` 修复，本次复测 500 响应头与 body 的 request_id 一致 | 见 [P0 独立验收报告](P0-acceptance.md) F2/F3；500 复测为 `req-boom / req-boom` | 不阻塞 P1 本地开发；打发布 tag 前必须补 Docker/CI 证据 |

> F1 的正常 CLI 路径不会触发（`taxonomy import` 的 parser 已拒绝 4 主标签）；但它绕过了“确认即保证结构合法”的服务边界，P2 若直接用仓储构造 draft 会踩到。  
> F2 是静默数据丢失风险：用户以为 6 个标签都被接受，实际只入库 5 个。  
> F3/F5 是数据完整性风险，不是当前测试失败；建议至少在 P3 前明确由哪个服务层负责。

---

## 6. 分项判定

| 小阶段 | 判定 | 说明 |
|--------|------|------|
| P1-S1 实体/状态机/不变量 | **通过** | 正常路径与边界测试通过；实体不可变、状态机白名单、核心不变量成立 |
| P1-S2 迁移/仓储/事件 | **通过（有 residual risk）** | 迁移、约束、事务、追加写通过；F3/F5 是仓储边界缺口，建议 P1-S3.1 或 P3 前处理 |
| P1-S3 文档解析/版本/CLI | **有条件通过** | validate/import/confirm/archive/diff 主流程通过；F1/F2/F4 需修复 |
| P1 整体 | **未完成** | S4–S6 尚未实现，本报告只验收 S1–S3 |

---

## 7. 建议的关闭条件

1. 修复 F1：`confirm_taxonomy` 调用 `ensure_taxonomy_shape`，并加正反例测试；
2. 修复 F2：格式错误的 `## Txx` 标题必须报错并定位行号；
3. 修复 F4：文档 CLI 捕获配置 `ValidationError`，输出 `PAPERSTEWARD_*` 字段提示；
4. 决定 F3/F5 的归属：
   - 若 P1 内修：在 `EventRepository.append` / `PaperRepository.transition` 加时间校验，在仓储增加聚合标签写入校验；
   - 若 defer：在阶段文档明确“由 P3/P4 的 classification service 负责”，并加 TODO/测试计划；
5. 修复后重跑：`make check`、`make test-integration`、`make migrate-check`、`make e2e`、`make demo`、`make secret-scan`；再更新 handoff。

完成上述条件后，可将 P1-S1–S3 标记为完全通过；在此之前建议保持“正常路径通过、契约边界待修”的状态，不创建 P1 阶段 tag。

---

## 8. 验收执行记录

- 验收人：独立 coding agent（未修改实现代码）
- 验收命令：`make check`、`make test-integration`、`make migrate-check`、`make e2e`、`make demo`、`make secret-scan`、`pytest -q`
- 额外检查：全新 clone（默认 Python 3.14）、P1-S1 实体/状态机/不变量边界、P1-S2 迁移与 DB 约束、P1-S3 CLI 全流程、malformed taxonomy、confirm shape、event backdating、invalid config、tag 聚合限制
- 验收产物：本报告
