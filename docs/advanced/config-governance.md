# 全局权重治理：预览、版本与确认

管理全局评分配置（scoring.yaml）的变更。**保存/导入新版本不等于激活**：
必须先预览 Top-N 排序影响，再显式确认；确认后在生效日使用新配置，并重算该日静态
快照。自定义排序视图（`view_config`）与此完全隔离。

## 流程

```bash
# 1) 预览：写入一条 previewed 记录，不改配置/静态快照
.venv/bin/papersteward config preview \
  --set static.relevance=0.50 \
  --set static.extra_value=0.30 \
  --date 2026-10-22 \
  --json

# 2) 确认：重新计算并核对 preview hash，写配置版本 + 生效日静态快照
.venv/bin/papersteward config confirm <change_id> --by local-user --note "确认 Top-N 变化"

# 3) 审计
.venv/bin/papersteward config changes
.venv/bin/papersteward config show <change_id>
```

- `config preview` 支持 `--set PATH=VALUE`（可重复）或 `--file scoring.yaml`；
  `--version` 缺省按最后一段数字自动 +1（如 `1.0.0 → 1.0.1`）。
- **只允许线性权重字段**：`static.weight/relevance/authority/extra_value`、
  `dynamic.weight/freshness/hunger`、`quasi_dynamic.weight`。`gates`、`stability`、
  `dynamic.half_life_days`、`quasi_dynamic` 子权重等非权重字段一律 400 拒绝；
  完整配置文件预览也会比较非权重字段，不允许借治理入口改候选集/EMA/日报/U 算法。
- 预览日 `--date` 缺省 runtime timezone 的今天（API/CLI 均同）；生效日 `--effective-on` 缺省预览日 +1 天。生效日必须
  晚于预览日，且晚于所有已有静态快照日期。
- 预览记录保存完整 proposed 配置、旧/新组件权重、逐篇旧/新名次与优先级、Top-N 明细
  和 `preview_hash`；**不写评分配置、不写静态快照、不激活**。
- 确认时重新读取当前队列并重算影响；队列或基线版本变化会使 hash 不匹配并返回 409，
  必须重新预览。确认成功才写新配置版本、`config.activated` 事件与生效日静态快照。
- 确认人必填；备注可选。同一变更不能重复确认；配置版本不能与已有版本同名。
- 生效日之前的队列/静态快照继续用旧配置；生效日及之后用新配置。旧的日快照不被改写。

## 激活语义

- `bound_context` 按目标日期选择：取 `effective_on ≤ 目标日` 的最新 confirmed 变更；
  没有已生效变更时，使用初始化确认写入的评分版本；若存在已确认但尚未生效的变更，
  则使用其 `base_version`，避免提前读到未来配置。所选配置仍会复核，保证初始化证据未漂移。
- 确认时为生效日追加每个已有静态快照文献的新版本静态分：
  `S_new = R·wR' + A·wA' + E·wE'`（R/A/E 来自最近静态快照，不重新调用 LLM）。
  这保证生效日队列有匹配新配置的静态分；档案/分类法版本保持原快照的值。
- 预览使用当前合格队列的六组件分数（R/A/E/F/H/U）和新展开权重重排，不扩大候选集；
  R 门槛、特殊桶排除、EMA/日快照规则不由本流程修改。预览的名次按
  `compute_priority` 的 `rank_score` 计算（新静态快照 input_hash 会触发 reset），
  不再用 priority 冒充实际队列名次；同时保留 `new_priority` 供解释。
- 确认后、生效日前新处理/提升的文献：`process`/`shelf promote` 会为每个已确认
  pending activation 补写生效日静态快照（R/A/E 线性重算，不重跑 LLM），避免生效日
  `static_version_mismatch` 把新文献挤出队列。确认时只覆盖当时已存在的快照。
- 静态分重算不会改变历史日期的快照；归档文献跳过重算，旧日期仍可解释。
- `config import` 仍只保存版本，不激活；视图保存/预览永远不会写全局配置。

## API

```bash
curl -s -X POST localhost:8000/config/scoring/preview \
  -H 'content-type: application/json' \
  -d '{"overrides":{"static.relevance":0.50,"static.extra_value":0.30},"score_date":"2026-10-22"}'

curl -s -X POST localhost:8000/config/scoring/changes/<id>/confirm \
  -H 'content-type: application/json' \
  -d '{"confirmed_by":"local-user","note":"确认 Top-N 变化"}'

curl -s localhost:8000/config/scoring/changes
curl -s localhost:8000/config/scoring/changes/<id>
```

- `POST /config/scoring/preview` 接受 `overrides` 或完整 `config`（二选一），可选
  `version/score_date/effective_on/limit`；返回 previewed 变更。
- `POST /config/scoring/changes/{id}/confirm` 返回 confirmed 变更和
  `static_recomputed` 篇数。
- 非法配置/日期 400，版本冲突、预览过期或重复确认 409，未知变更 404。
- 预览/确认均使用标准错误信封；不返回密钥或完整论文正文。

## 数据与回滚

- 迁移 `0014` 新增 `scoring_change`：status、base/config version、preview_day、
  effective_on、proposed/preview JSON、preview_hash、确认人/时间/备注；迁移 `0015`
  为特殊桶人工改档补 `special_tier_snapshot.reviewed_by`（见[特殊桶](shelves.md)）。
- 当前 head 为 `0018`；0018 的降级不恢复历史延期状态；再降级会移除论文分析表，再重建旧版初始化状态，再移除后续表。
  降级到 0014 只删除档位审核人列，再降至 0013 才删除权重变更记录。
  已保存的评分配置版本与静态快照不回退。
  若需撤销一次激活，应新建更高版本并按预览/确认流程生效，不删除历史。
- 不做自动调参、自动确认、权重滑块 UI、多用户权限；不做全局配置的原地覆盖。
