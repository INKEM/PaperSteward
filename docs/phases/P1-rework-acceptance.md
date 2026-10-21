# P1 返工验收报告（F1–F4 / CI 闭环）

> **验收对象**：P1 整体验收返工（F1–F4）与 CI 闭环  
> **验收 commit**：`bca7a7a`（包含返工提交 `dbdbd26`）  
> **验收日期**：2026-10-21  
> **验收方式**：不修改实现代码；独立运行阶段门、全新 clone、F1–F3 手工复现、Docker 运行复核、GitHub Actions API 复核  
> **结论**：**通过（Accepted）**。P1-S1–S6 可以关闭；仅保留 F5（httpx/Starlette 弃用警告）与 F3 回归断言缺失两项非阻断债务。

---

## 1. 验收结论

| 返工项 | 状态 | 独立证据 |
|--------|------|----------|
| F1 `save_scoring_config` 校验 | **已修复** | 非法配置抛 `ConfigError`；`config` 表与 `event` 表均无写入 |
| F2 特殊桶 scoring schema | **已修复** | 新增 `special_scoring`（tier、无 P）；主题桶 `scoring` 仍要求 priority；freeze 测试同步 |
| F3 provider 失败 usage | **已修复** | 最终 `LLMTimeoutError.usage` 可读取：calls=3、retries=2 |
| F4 Docker/CI | **已闭环** | 容器 UID 10001、healthy、`/health`、seed 后 `/papers`；CI run [39610871808](https://github.com/INKEM/PaperSteward/actions/runs/39610871808) 在 `bca7a7a` 上 3 个 job 全绿 |
| F5 httpx/Starlette 警告 | **保留（非阻断）** | `make check` 仍有 `StarletteDeprecationWarning`；不影响功能与阶段门 |
| F3 回归测试 | **测试债（非阻断）** | handoff 声称的 `test_all_provider_attempts_fail_with_usage` 不存在；现有 `test_all_provider_attempts_fail` 未断言 `usage`，但代码行为已手工验证 |

**总体判定**：**P1-S1–S6 通过，P1 可以关闭**。F5 与 F3 测试断言属于维护债，不阻塞 P1 关闭；建议在下一维护窗口补齐。

---

## 2. 阶段门证据

在 `bca7a7a` 上执行：

```text
make check               → 339 passed, 1 deselected
make test-integration    → 102 passed
pytest -q                → 340 passed
make migrate-check       → upgrade/downgrade/upgrade OK
alembic check            → No new upgrade operations detected（0002 head）
make e2e                 → 1 passed
make demo DEMO_PORT=8041 → health ok；papers p001（discovered）；API 停止
make secret-scan         → OK
coverage                 → core/documents/storage/scoring/schemas/llm 合计 97%
```

全新 clone（默认 Python 3.14.7）执行 `make bootstrap` 后，check / integration / migrate / e2e / demo / secret / `docker compose config` 全部通过，`git status --short` 为空。

---

## 3. F1–F3 独立复核

### F1：配置 service 写入前校验

- 构造非法 `ScoringConfig`（`static.relevance=0.40`）；
- `save_scoring_config()` 抛 `ConfigError`；
- `ConfigRepository.versions("scoring") == []`；
- `EventRepository.recent() == []`。

结论：非法配置不再进入业务表，也不再写 `config.updated` 事件。

### F2：特殊桶评分 schema

- `SCHEMA_MODELS` 现包含 `classification / summary / scoring / special_scoring / event`；
- `special_scoring` 的 required 为 tier、无 priority；
- 带 `tier`、无 `priority` 的载荷通过；
- 带 `priority` 的 special 载荷被拒绝；
- 主题桶 `scoring` 仍要求 `priority`，两类 schema 分工明确；
- `test_p1_freeze` 已同步冻结两个 schema 的 required 集合。

### F3：provider 失败 usage

```text
LLMTimeoutError.usage = {
  "calls": 3, "retries": 2, "repairs": 0,
  "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0
}
```

成功调用、修复调用与修复失败的 usage 行为未回归。

**测试债**：handoff 引用的 `test_all_provider_attempts_fail_with_usage` 在代码库中不存在；实际测试 `test_all_provider_attempts_fail` 只断言 `provider.call_count == 3`，未断言 `exc.usage`。建议补一条断言或重命名测试；本轮以手工验证确认代码行为。

---

## 4. F4 Docker + CI 闭环

### 4.1 本地 Docker 运行复核

使用现有 `papersteward:dev` 镜像（构建时间 2026-10-21T19:05:27+08:00）以独立 Compose project 启动：

```text
docker compose -p p1accept up -d --no-build
container health → healthy
id → uid=10001(papersteward) gid=10001(papersteward)
/app/src/papersteward/core/entities.py → readable（644 root）
/app/alembic.ini → 644；/app/docker-entrypoint.sh → 755
alembic current → 0002 (head)
papersteward demo seed → inserted p001
/health → 200 {"status":"ok","database":"ok"}
/papers → 200，total=1，status=discovered
docker compose -p p1accept down -v → 正常清理
```

本地镜像的源码构建时间早于 F1–F3 代码返工，因此本地运行主要验证容器运行、非 root 读权限、迁移与 API 链路；**当前 commit 的容器证据以 CI docker job 为准**。

### 4.2 CI 复核

用户已手动确认 CI 可闭合；GitHub Actions API 独立复核：

- run [39610871808](https://github.com/INKEM/PaperSteward/actions/runs/39610871808)
- head：`bca7a7a`（包含 F1–F3 返工）
- 3 个 job 全部 `success`：
  - `quality (py3.11)`：bootstrap / static checks / migration / e2e / secret scan；
  - `quality (py3.12)`：同上；
  - `docker compose smoke`：validate compose / build image / start stack / wait healthy / smoke endpoints / stop stack。
- 结论：当前 commit 的 Docker build/run 与 CI 质量门均通过。

---

## 5. 剩余非阻断债务

| ID | 内容 | 建议 |
|----|------|------|
| F5 | `StarletteDeprecationWarning`（httpx → httpx2） | 依赖升级窗口处理；不影响 P1 关闭 |
| F3-test | F3 的 usage 断言未落地，handoff 引用的测试名不存在 | 在 `test_all_provider_attempts_fail` 中增加 `exc.usage.calls == 3` / `retries == 2`，或新增命名测试 |
| 文档 | `P1-acceptance.md` 的初始结论（336/101/337、Docker 未通过）是返工前历史记录 | 已保留为历史；最终结论以本报告为准 |

---

## 6. 最终判定

**P1 通过，可以关闭，也可创建 `p1-done` tag。**  
本轮独立验收确认：F1–F4 均闭环；F5 与 F3 的测试断言是维护债，不构成功能或阶段门阻断。P2 可以从 [P1 handoff](P1-domain-documents-llm.md) 直接开始。

---

## 7. 验收执行记录

- 验收人：独立 coding agent（未修改实现代码）
- 验收命令：`make check`、`make test-integration`、`pytest -q`、`make migrate-check`、`alembic check`、`make e2e`、`make demo`、`make secret-scan`、`docker compose config`
- 额外检查：全新 clone（默认 Python 3.14）、F1–F3 手工复现、Docker UID 10001/healthy/health/papers、GitHub Actions run/job API
- 验收产物：本报告
