# P3 独立验收报告

> **验收对象**：P3 采集、去重与硬过滤（P3-S1–S7）  
> **验收 commit**：`a93d96a`（`feat(p3-s7): persist screened ingestion and auditable run reports`；已合并到 main `9fe91dc`）  
> **验收日期**：2026-10-22  
> **验收方式**：不修改实现代码；独立运行阶段门、全新 clone、Docker build/run、GitHub Actions 复核、离线来源/门禁/幂等/失败路径检查  
> **结论**：**通过（Accepted）**。P3-S1–S7 的本地阶段门、Docker 运行与 CI 均通过；未发现 P3 阻断缺陷。P3 可关闭。
>
> **后续跟进（2026-10-22）**：§5 的四项非阻断观察已在验收后完成优化并通过阶段门，
> 详见 [P3-ingestion.md](P3-ingestion.md) 的“P3 验收后非阻断优化”与“P3 优化验证”。
> 本报告保留验收时点的结论与证据。

---

## 1. 结论摘要

| 项目 | 结果 |
|------|------|
| `make check` | **通过**：156 文件格式化；80 源文件 strict mypy；**684 passed / 4 e2e excluded** |
| `make test-integration` | **通过**：**224 passed** |
| `make e2e` | **通过**：**4 passed**（walking skeleton、onboarding API/CLI、完整采集） |
| `make migrate-check` | **通过**：base → 0005 → base → 0005 |
| `make demo` | **通过**：临时库迁移、seed、`/health`、`/papers` |
| `make secret-scan` / `git diff --check` | **通过** |
| 全新 clone（默认 Python 3.14） | **通过**：check / integration / migrate / e2e / demo / secret |
| Docker build/run（当前代码） | **通过**：0005 head；UID 10001；healthy；doctor、`/health`、`/papers`、`/onboarding` 正常 |
| CI | **通过**：branch run [39709657832](https://github.com/INKEM/PaperSteward/actions/runs/39709657832)（`a93d96a`）与 main run [39709826878](https://github.com/INKEM/PaperSteward/actions/runs/39709826878)（`9fe91dc`），3 个 job 全绿 |
| P3 tag | **未创建**；验收通过后可按发布流程补 `p3-done` |

---

## 2. 阶段门证据

### 2.1 当前工作区

```text
make check PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p3-accept-check'
  → 684 passed, 4 deselected；ruff/mypy 通过

make test-integration PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p3-accept-int'
  → 224 passed

TMPDIR=/dev/shm make migrate-check
  → base → 0001 → 0002 → 0003 → 0004 → 0005 → base → 0005

make e2e PYTEST='.venv/bin/pytest --basetemp=/dev/shm/p3-accept-e2e'
  → 4 passed

make demo DEMO_PORT=8055
  → /health ok；/papers 返回 p001（discovered）

make secret-scan
  → OK
```

SQLite 使用 `/dev/shm` 临时目录，未修改同步设置，也未写入 `var/papersteward.db`。

### 2.2 全新 clone

默认 Python 3.14.7，执行 `make bootstrap` 后：

- `make check`：684 passed（4 e2e 排除）；
- `make test-integration`：224 passed；
- `make migrate-check`：0005 往返通过；
- `make e2e`：4 passed；
- `make demo`：health/papers 正常；
- `make secret-scan`：通过；
- `git status --short`：空。

---

## 3. Docker 与 CI

### 3.1 Docker

使用当前 `a93d96a` 代码重新执行 `docker compose build`，构建成功；独立 Compose project 启动：

```text
container health → healthy
id → uid=10001(papersteward) gid=10001(papersteward)
alembic current → 0005 (head)
papersteward doctor → 配置/DB/迁移全部 OK
/health → 200 {"status":"ok","database":"ok"}
/papers → 200 {"items":[],"total":0}
/onboarding → 200 {"stage":"seeds","revision":0,"next_gate":null}
docker compose down -v → 正常清理
```

结论：P3 代码、0005 迁移和非 root 容器运行均正常。

### 3.2 CI

GitHub Actions API 复核：

- PR run [39709657832](https://github.com/INKEM/PaperSteward/actions/runs/39709657832)：head `a93d96a`，`quality (py3.11)`、`quality (py3.12)`、`docker compose smoke` 全部 success；
- main run [39709826878](https://github.com/INKEM/PaperSteward/actions/runs/39709826878)：head `9fe91dc`（P3-S7 合并提交），同样 3 个 job 全绿。

P3 的 Docker/CI 发布门已闭合；P2 的后续 CI run 也已成功（见 §6）。

---

## 4. P3 契约与功能复核

### 4.1 来源边界与适配器

- `SourceAdapter` / `QuerySpec` / `PaperCandidate` / `SourceError` 冻结；候选不携带库内 ID/状态/评分。
- arXiv、OpenAlex、Semantic Scholar 全部使用离线 cassette/MockTransport 覆盖：分页、空结果、缺字段、损坏响应、限流/超时、预算、重复 ID、日期/字段查询与 host 边界。
- `SourceHTTP`：HTTPS host 白名单、禁用代理/重定向、超时、有限退避、`Retry-After`、请求预算、8 MiB 响应上限、24 小时内存缓存和统计。
- CLI 默认要求 `--fixture`；真实 HTTP 必须显式 `--live`，未发现隐式真实网络调用。

### 4.2 查询生成

- 只读取 G1 事件精确选定的 confirmed profile，并校验正文/解析缓存一致；不读取无关 latest。
- 子方向按优先级稳定排序，补充 methods/data/关注列表，保序去重并受预算截断。
- 空档案、草案、漂移和非法查询预算均明确拒绝。

### 4.3 去重与硬过滤

- DOI/arXiv/标题/作者年份/摘要近似的确定性去重；强 ID 冲突不被弱匹配桥接。
- `canonical_id` 稳定；正式版本字段优先，保留最早日期与全部来源。
- 硬过滤覆盖语言、日期、类型、排除词、作者/venue 黑名单、掠夺性 venue 和经典文献日期豁免；未知字段不被伪造为不合格。
- 已验证已归档/已拒绝/已评分元数据不被覆盖；种子/校准记录不被改写。

### 4.4 Ingest、运行报告与门禁

- G1/G2/G3/G4 精确绑定；暂停、安静时段、配置/报告漂移在调用来源前或落库前拒绝。
- 候选经 discovered → deduped → screened/rejected；无评分、分类、入队、推送、归档写入。
- `paper_source` 追加不可变来源版本；相同原始版本重跑不重复，来源更新保留库内 ID。
- Run 记录成功/失败/partial、来源请求/缓存统计、过滤原因、绑定版本和耗时；部分来源失败隔离，全部失败为 failed。
- savepoint 撤销半批状态/来源/事件，但保留失败报告；外层 `session_scope` 可整体回滚。
- CLI 实测连续两次采集新增数 1→0、来源记录不重复；`runs show --last` 返回最新报告；无 Rating/ScoreSnapshot/ArchiveSnapshot 写入。

### 4.5 执行门禁

`test_every_pre_g4_stage_blocks_all_execution_paths` 与真实 Uvicorn 覆盖：

- G4 前 `PaperRepository.add` 直接写 queued/pushed/archived 拒绝；
- `PaperRepository.transition` 到上述状态拒绝；
- `ArchiveSnapshotRepository.add` 拒绝；
- 拒绝时不写 paper 状态/事件；G4 后放行。

P3 ingest 本身也要求 ACTIVE，不存在绕过 G4 的采集路径。

---

## 5. 非阻断观察

未发现 P3 阻断缺陷。以下为后续可优化的低优先级点：

1. **早期 `discovered`/`deduped` 状态的存量文献**在重跑时若没有新的过滤原因，当前实现不会主动推进到 `screened`；正常 P3 新文献在同一事务内会完成 `discovered→deduped→screened/rejected`，该问题只影响直接构造或异常中断后的存量状态。建议后续补一条回归/状态收敛逻辑。
2. `SourceHTTP` 内存缓存按条数限制（128 条，每条最多 8 MiB），未按总字节数限制；本地单用户场景风险低。
3. OpenAlex 作者/venue/团队名称消歧只读取第一页（100 条）；同名极多时可能返回 unsupported_query，属于保守失败。
4. CLI 的 MockTransport fixture 对多页响应每次返回同一页，多页 cassette 会因重复 ID 失败；P3 测试使用单页 fixture，真实分页由适配器契约测试覆盖。

以上均不影响 P3 阶段门或发布验收。

---

## 6. 继承债务与上游状态

- **P1 F5**：`StarletteDeprecationWarning`（httpx → httpx2）仍在，非阻断依赖债。
- **P1 F3 测试债**：provider 失败 usage 的独立断言仍待补。
- **P2 CI**：后续 run [39643102708](https://github.com/INKEM/PaperSteward/actions/runs/39643102708)（`16d9b59`）3 个 job 全绿，P2 的 CI 缺口可视为已关闭；P2 tag 仍未创建。
- P3 未使用真实 API/LLM/用户密钥；未实现自动调度、语义 embedding、摘要/分类/评分、正式推送/归档。

---

## 7. P4 交接

P3 已提供：

- `SourceAdapter`、`QuerySpec`、`PaperCandidate`、`SourceError`；
- arXiv/OpenAlex/Semantic Scholar cassette 与 `SourceHTTP` 边界；
- canonicalization/DedupeGroup；
- screened 候选池、`paper_source` 原始来源版本、Run 报告与 CLI；
- G1/G2/G4 精确配置绑定与 G4 执行门禁。

P4 可以按 [P3 handoff](P3-ingestion.md) 开始；新字段/表必须新增迁移（当前 0005），真实模型/来源仍需显式授权与 fake/cassette 测试。

---

## 8. 最终判定

**P3-S1–S7 通过，P3 可以关闭。** Docker、CI、全新 clone 与本地阶段门均已验证；未发现 P3 功能阻断缺陷。P3 阶段 tag 可按发布流程补打，本轮未擅自创建。

---

## 9. 验收执行记录

- 验收人：独立 coding agent（未修改实现代码）
- 验收命令：`make check`、`make test-integration`、`make migrate-check`、`make e2e`、`make demo`、`make secret-scan`、`docker compose config/build/up`
- 额外检查：全新 clone（Python 3.14）、Docker 当前镜像、GitHub Actions branch/main run 与 job 状态、G4 门禁、ingest 幂等/失败/来源隔离测试
- 验收产物：本报告
