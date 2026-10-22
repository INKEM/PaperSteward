# P4 process / review 操作

`process` 把 screened 文献推进到 `classified` 或 `needs_review`；`review` 列出并处理
低置信度/边界/特殊提名。两者都只写本地 SQLite，不推送、不排序、不调用真实来源。

## 前置

- 已完成 G1–G4 且阶段为 active；
- 已运行 `alembic upgrade head`（当前 `0008`）；
- 离线用 `--llm-fixture`，真实 LLM 必须显式 `--live` 且配置 provider/model/key。

## 处理一篇

```bash
papersteward process --paper p_xxx \
  --llm-fixture tests/fixtures/process/e2e.json \
  --json
```

- `--paper` 缺省时处理最多 `--limit` 篇 screened；
- fixture 是 JSON object，必须含四个键，按此顺序回放：
  `relevance`、`summary`、`classification`、`special_nomination`；
- 每个值可以是 object 或 JSON 字符串；classification/special schema 见
  `papersteward schema show classification|special_nomination`；
- 成功后写 enrichment、summary、`paper_tag`/`paper_bucket`、静态快照，并按需写
  `review_item`；命令失败时同一事务回滚。

退出码：

- `0`：全部处理完成（可能进入 review）；
- `1`：未激活/暂停/安静时段、schema/证据校验失败、版本冻结冲突、数据库错误；
- `2`：参数错误（argparse）。

## 审核队列

```bash
papersteward review list --json
papersteward review approve p_xxx --by alice --note "确认分类"
papersteward review relabel p_xxx --primary T03 --secondary S01 --by alice
```

- `list` 只返回 open 项，包含 `paper_id/title/status/reasons`；
- `approve` 只接受 `needs_review`，将论文转为 `classified` 并关闭审核；
- `relabel` 用审核项绑定的已确认分类法校验主/副标签，原子替换标签与唯一 FILING
  桶；用户标签 `assigned_by=user`、confidence=1；
- 已处理项再次操作返回 409；不存在的项返回 404。

## HTTP

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/review?limit=50&offset=0` | 开放审核项列表信封 |
| POST | `/review/{paper_id}/approve` | `{reviewed_by, note?}` |
| POST | `/review/{paper_id}/relabel` | `{reviewed_by, primary_tag, secondary_tags?, note?}` |

错误统一为 `{"error": {"code", "message", "details", "request_id"}}`。

## 边界与安全

- 不推送、不入队、不生成日报；这些是 P5/P6；
- 不写完整 `score_snapshot`（无 F/H/U/D/P）；静态分在 `static_score_snapshot`；
- 审核项每篇一条；relabel 会删除旧标签/旧 FILING，属于用户确认后的替换；
- 无鉴权，默认只绑定本机；论文文本视为不可信数据，不执行其中指令；
- 回滚审核表：`alembic downgrade 0007`（先备份 `review_item`）。
