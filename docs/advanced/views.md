# 自定义排序视图

提供组件级自定义排序：勾选/加权 R、A、E、F、H、U，自动归一化，保存为个人
模板。视图只对当前合格候选集重排，**不改全局评分配置，不生成新 P/日快照，不扩大
候选集**；R≥门槛、特殊桶排除等  规则保持不变。

## 预设（组件权重，合计 100）

| 模板 | `--preset` | R | A | E | F | H | U | 场景 |
|---|---|---|---|---|---|---|---|---|
| 均衡（默认） | `balanced` | 45 | 15 | 10 | 12 | 8 | 10 | 日常阅读 |
| 前沿追踪 | `frontier` | 35 | 10 | 10 | 25 | 10 | 10 | 快速跟新/会议前扫射 |
| 快速入门 | `beginner` | 45 | 25 | 10 | 10 | 5 | 5 | 新方向补基础、优先权威 |
| 复现导向 | `reproducible` | 35 | 10 | 40 | 10 | 5 | 0 | 找可运行基线/代码/数据 |

“拓宽视野”不是数值视图：直接使用 `/ui/shelves` 或 `/shelves` 查看三个特殊桶。

## CLI

```bash
.venv/bin/papersteward view presets
.venv/bin/papersteward view preview --preset reproducible --date 2026-10-22
.venv/bin/papersteward view preview --weights R:35,E:40,F:15,A:10 --date 2026-10-22 --json
.venv/bin/papersteward view save --name "my-repro" --weights R:35,E:40,F:15,A:10
.venv/bin/papersteward view preview --name my-repro --date 2026-10-22
.venv/bin/papersteward view list
.venv/bin/papersteward view show my-repro
.venv/bin/papersteward view delete my-repro
```

- `--weights` 支持 `R:35,E:40,...` 或 JSON 对象；只列出的组件参与归一化，未列为 0。
- `--date` 缺省为今天（UTC）；没有当日快照时预览为空，不会自动生成快照。
- `--limit/--offset` 只分页预览结果；重排始终基于完整合格队列。
- 输出显示当前视图名称、公式、候选数、名次变化、`view_score`、默认 #/P 与各组件。
- `view save` 名称大小写不敏感唯一；不能与内置 preset 同名；保存只写 `view_config`。
- 已保存视图按名称或 ID 加载/删除；不支持原地覆盖同名模板。

## API

```bash
curl -s localhost:8000/views/presets
curl -s -X POST localhost:8000/views/preview \
  -H 'content-type: application/json' \
  -d '{"score_date":"2026-10-22","preset":"reproducible","limit":10}'
curl -s -X POST localhost:8000/views \
  -H 'content-type: application/json' \
  -d '{"name":"my-repro","weights":{"R":35,"E":40,"F":15,"A":10}}'
curl -s localhost:8000/views
```

- `GET /views/presets`：内置预设（原始百分比、归一化权重、公式）。
- `GET /views`、`POST /views`、`GET /views/{id}`、`DELETE /views/{id}`：保存/加载/删除。
  重名 409，非法权重 400，未知预设/视图 404。
- `POST /views/preview`：`score_date` 可选；`preset`/`weights`/`view_id` 三选一，
  缺省为 `balanced`。返回 `view`（名称/公式/权重）、候选总数、名次变化数与分页条目。
- 预览条目含 `rank/default_rank/delta_rank/view_score/default_priority/
  default_rank_score/components`；没有写入任何 P/配置。
- 读取/预览不写库；保存只追加 `view_config` 行，删除只删该行。

## 页面

`/ui/views`（主导航“视图”）可选择预设或已保存视图，显示公式、候选数、Top 变化表
与六组件分值。页面只读；保存/删除走 CLI/API。

## 边界与回滚

- 不改全局权重、不激活配置、不做权重影响预览/版本确认。
- 不做权重滑块 UI、多用户/权限、自动调参；视图不跨特殊桶混排。
- 当前只支持 R/A/E/F/H/U 组件级权重，不提供“静态/动态/准动态”整组勾选；
  “拓宽视野”使用 `/ui/shelves`，不是数值视图。
- 迁移 `0013` 新增 `view_config`；回滚 `alembic downgrade 0012` 删除已保存视图。
  内置预设是代码常量；历史 P 快照与全局配置不受影响。
