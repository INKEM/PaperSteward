# 特殊桶只读视图

P7-S3 提供三个特殊桶的只读 API、CLI 与本地页面。它们展示 S2 的档位、理由、
证据与 kind 专属解释，不重新计算 P、不返回 V/rank_score，也不写库。

## 三个桶

| kind | 展示名 | 周配额（默认） | 专属解释 |
|---|---|---|---|
| `review` | 跨层综述 | 3 | 覆盖层级、跨层/证据理由 |
| `distant` | 远邻领域 | 2 | 四段桥接假设 |
| `classic` | 经典/奠基 | 1 | 经典依据（foundational/citation/standard/award/expert） |

配额来自 `bucket.quota`；未配置时使用默认值。视图按 review → distant → classic
分组，组内只按 high/medium/low/未评估排列，不做跨桶排名。

## API

```bash
curl -s localhost:8000/shelves
curl -s "localhost:8000/shelves/distant?limit=20&offset=0"
curl -s "localhost:8000/shelves/review?tier=medium"
curl -s localhost:8000/shelves/review/p001
```

- `GET /shelves`：三个桶概览（`total/high/medium/low/unrated/quota/active`），
  使用 `items/total/limit/offset` 信封；桶尚未创建时返回空桶摘要，不写库。
- `GET /shelves/{kind}`：分页内容；`tier=high|medium|low|unrated` 可过滤，
  非法 kind 返回 404，非法 tier 返回 422。
- `GET /shelves/{kind}/{paper_id}`：单篇详情；不在该桶或已归档返回 404。
- 条目包含主标签/副标签、档位、理由、逐字证据、档位说明、桥接假设/覆盖层级/
  经典依据、评估来源与时间、改档备注；**不包含** `value`、`priority`、
  `rank_score`。`excluded_from_priority` 始终为 true。
- 未评估的已归属文献仍会显示（`tier=null`、`needs_assessment=true`），并提示运行
  `shelf assess`；读取不会自动补评估。
- 只展示当前特殊桶仓库中的文献：`shelved/pushed/reading/done`；`archived` 走归档页，
  `rejected` 不展示。空桶返回空 `items`，不生成占位文献。

## CLI

```bash
.venv/bin/papersteward shelf list
.venv/bin/papersteward shelf list --kind review --tier medium --explain
.venv/bin/papersteward shelf distant --explain
.venv/bin/papersteward shelf classic
.venv/bin/papersteward shelf show p001 --json
```

- `shelf list`：无 `--kind` 时输出三个桶概览；指定 `--kind` 时输出内容。
- `shelf distant`/`shelf classic`：对应桶的快捷列表；`--explain` 显示档位说明与
  桥接假设/覆盖层级/经典依据。
- `shelf show <paper_id>`：任意特殊桶文献详情；不在特殊桶返回非 0。
- JSON 输出与 API 字段一致，可直接脚本处理；不含 V/P/rank。

`shelf review` 仍是 S1 的待审提名队列；查看综述桶请用
`shelf list --kind review` 或 `shelf show <paper_id>`。

## 页面

浏览器打开 `/ui/shelves`（主导航“特殊桶”）。页面按三个桶分区显示配额、档位分布、
标题、档位、理由、证据与专属解释；空桶显示“暂无内容（不占位）”。点击标题进入
文献详情；特殊桶详情不再调用 P 解释，而是显示档位与证据。

## 重新打开被拒绝的提名

```bash
.venv/bin/papersteward shelf reopen p001 --kind review --by local-user --note "重新考虑"
```

- 只允许 `classified` 文献的 `rejected` 提名；把该提名受控回到 `pending`，清空本次
  拒绝对话字段并追加 `special_nomination.reopened` 审计事件；不重跑 LLM、不改主标签/
  Filing/状态。其他 kind 的待审提名可继续存在。
- 重新打开后可按正常 `shelf accept` 流程接受；若产品决定拒绝不可逆，不应使用该命令。

## 提升回主题桶

```bash
.venv/bin/papersteward shelf promote p001 --by local-user --note "人工确认可回主队列" --json
```

- 只允许 `shelved` 的特殊桶文献；文献必须有唯一主标签、对应激活主题桶和已通过
  主队列相关度门槛（R≥max(55, min_relevance_for_push)）。否则返回冲突，不移动。
- 把 filing 从特殊桶替换为主标签主题桶，状态 `shelved → classified`，写
  `paper.classified` 与 `paper.bucket_moved` 事件；特殊桶档位/历史保留。
- 若最近静态快照已匹配当前配置/档案/分类法版本，直接复用；否则用最近 R/A/E
  按当前静态权重追加一条新日期快照。若存在已确认但未生效的权重变更，还会为每个
  pending activation 补写生效日新版本快照。下一次日快照即可把文献纳入 P 排序。
- 提升不会改写历史 P 快照；主题桶→特殊桶的手动降级尚未提供独立命令，当前仍通过
  `shelf accept`（仅 classified 阶段）完成。
- 人工 `shelf tier` 现在持久化 `reviewed_by`；`shelf show --json` 的
  `tier_reviewed_by` 与 `shelf tier --json` 的 `reviewed_by` 可用于审计。

## 边界与回滚

- 不实现特殊桶推送、日报特殊槽位、档位编辑/配额修改（分别属于后续卡/已有 CLI）。
- 只读视图无迁移、无新依赖；回滚删除 API/CLI/页面代码即可，已保存的档位/配额/
  Filing 不受影响。
- 页面文本全部转义；原文链接只允许 http(s)。V 不进入展示契约。
