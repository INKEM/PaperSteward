# 配置与工作区

模型连接优先在浏览器 **设置** 页完成，保存在当前工作区数据库，不需要 `.env`，也不需要重启。环境变量 / `.env` 保留给 CLI、容器和自动化等高级场景，作为数据库配置缺失时的回退。

## 用设置页配置模型

打开 `/ui/settings` 或首次设置第一步：

| 字段 | 说明 |
|---|---|
| API 地址 | OpenAI 兼容根地址，provider 自动追加 `/chat/completions`；非本机必须 HTTPS |
| API 密钥 | 非本机地址必填；本机 loopback 模型可留空。若 `.env` 同 origin 有密钥，未填写时会自动沿用并在设置页标注来源 |
| 模型名 | 必填，例如 `deepseek-chat`、`gpt-4o-mini` |
| 请求超时 | 单次请求硬超时，秒 |
| 知情同意 | 勾选后才能保存；保存即授权把论文标题、摘要及任务所需研究方向/分类规则发给该服务，生成草案还会发送研究材料、意见和上一版草案 |

保存会追加一条版本化 `llm_config` 记录并写入审计事件；密钥只返回掩码，不进入页面 HTML、日志或运行报告。再次保存时密钥留空会沿用已保存的值；工作区配置没有密钥而 `.env` 中同 origin 配置了密钥时，也会沿用该密钥（避免升级后 401）。非本机地址最终仍无密钥时拒绝保存。**测试连接** 用表单当前值发一次最小请求，不落库。

读取顺序：数据库最新 `llm_config` → 环境变量 / `.env`。数据库配置自带授权；环境变量路径仍要求 CLI 显式 `--live` 或网页保存一次设置。

## 环境变量（高级 / 容器）

配置来自启动目录中的 `.env` 或 `PAPERSTEWARD_*` 环境变量，环境变量优先。源码用户可复制根目录 `.env.example`；pipx 用户可在自己的启动目录创建 `.env`。不要把密钥写进研究资料、命令行参数或备注。

| 配置 | 默认 | 用途 |
|---|---|---|
| `PAPERSTEWARD_DATA_DIR` | `./var` | 数据目录 |
| `PAPERSTEWARD_DATABASE_URL` | 数据目录下 SQLite | 高级数据库地址 |
| `PAPERSTEWARD_API_HOST` | `127.0.0.1` | API 绑定地址 |
| `PAPERSTEWARD_API_PORT` | `8000` | API 端口 |
| `PAPERSTEWARD_TIMEZONE` | `Asia/Shanghai` | 确认草案时的默认业务时区 |
| `PAPERSTEWARD_LOG_FORMAT` | `json` | `json` 或 `console` |
| `PAPERSTEWARD_LOG_LEVEL` | `INFO` | 日志级别 |
| `PAPERSTEWARD_LLM_PROVIDER` | `fake` | 仅 CLI/测试；网页设置页固定 `openai_compatible` |
| `PAPERSTEWARD_LLM_BASE_URL` | `https://api.openai.com/v1` | 兼容 API 根地址 |
| `PAPERSTEWARD_LLM_MODEL` | 空 | 真实 provider 必填 |
| `PAPERSTEWARD_LLM_API_KEY` | 空 | 密钥，仅配置读取 |
| `PAPERSTEWARD_SEMANTIC_SCHOLAR_API_KEY` | 空 | 可选来源 key |

`papersteward start --data-dir 路径` 明确选择该目录下的 SQLite，优先于 `DATABASE_URL`；路径按当前启动目录解析。`--host` / `--port` 覆盖启动绑定；默认打开浏览器，`--no-browser` 关闭。`start` 会先占用端口、显式升级迁移，再启动；直接运行 ASGI 不自动迁移。

CLI 的 `doctor` / `process` 等命令使用配置中的数据目录，不自动继承上次 start 参数。例如先设置 `PAPERSTEWARD_DATA_DIR=./my-research` 并移除无关 `DATABASE_URL`，再运行 `papersteward doctor`。避免多个服务同时操作同一目录。

Docker Compose 固定容器数据路径 `/app/var`、本机端口 8000、命名卷；模型配置同样建议进入容器后通过浏览器设置页保存。若用环境变量注入，它们只作为数据库配置缺失时的回退。Docker 内的 `127.0.0.1` 指容器本身，本机模型需配置可达地址；不要绕过非本机 HTTPS 校验。

## 运行参数与评分

确认草案时写入 `onboarding_runtime`：时区、主标签目标占比、模型外发授权。新工作区不写每日推送时间、安静时段、暂停或投递渠道；旧字段保持可读，但不阻止手动挖掘、排序或阅读。

评分使用内置 `scoring.yaml` 生成的默认版本；保存新版评分配置不代表激活，更改全局权重走[预览与确认](advanced/config-governance.md)。阅读容量仅作软建议，不限制持久队列的篇数。

升级前停止服务并复制完整数据目录。Python 安装新版本后再次 start；Docker 重建并使用原卷。迁移失败保留日志并查[排障](troubleshooting.md)，不要删除数据库重试。回到旧版本优先恢复匹配版本的完整副本；降迁移可能删除记录，不能作为日常重试方法。


## 查看与重置工作区

设置页展示当前已确认的研究档案、分类标签与时区。需要反复测试时，进入“重置工作区 / 清空数据”，核对论文、反馈和材料数量，输入“重置工作区”提交。运行中不能重置；备份失败则保留原数据。

重置前会在数据目录的 `backups/` 保存权限为 0600 的完整 SQLite 备份，随后在单一事务中清空业务表，保留迁移版本。可勾选保留模型连接；取消勾选会删除数据库中保存的模型配置，环境变量仍可作为回退，但网页需重新保存授权。原导出文件与备份不会被删除。重置后无需重启，直接重新进行首次设置。
