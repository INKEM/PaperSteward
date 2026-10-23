# 配置与工作区

配置来自启动目录中的 `.env` 或 `PAPERSTEWARD_*` 环境变量，环境变量优先。源码用户可复制根目录 `.env.example`；pipx 用户可在自己的启动目录创建 `.env`。不要把密钥写进研究资料或备注。修改服务配置后重启。

| 配置 | 默认 | 用途 |
|---|---|---|
| `PAPERSTEWARD_DATA_DIR` | `./var` | 数据目录 |
| `PAPERSTEWARD_DATABASE_URL` | 数据目录下 SQLite | 高级数据库地址 |
| `PAPERSTEWARD_API_HOST` | `127.0.0.1` | API 绑定地址 |
| `PAPERSTEWARD_API_PORT` | `8000` | API 端口 |
| `PAPERSTEWARD_TIMEZONE` | `Asia/Shanghai` | 默认业务时区；运行遵循 G4 确认值 |
| `PAPERSTEWARD_LOG_FORMAT` | `json` | `json` 或 `console` |
| `PAPERSTEWARD_LOG_LEVEL` | `INFO` | 日志级别 |
| `PAPERSTEWARD_LLM_PROVIDER` | `fake` | 离线替身或 `openai_compatible` |
| `PAPERSTEWARD_LLM_BASE_URL` | `https://api.openai.com/v1` | 兼容 API 根地址 |
| `PAPERSTEWARD_LLM_MODEL` | 空 | 真实 provider 必填 |
| `PAPERSTEWARD_LLM_API_KEY` | 空 | 密钥，仅配置读取 |
| `PAPERSTEWARD_SEMANTIC_SCHOLAR_API_KEY` | 空 | 可选来源 key |

`papersteward start --data-dir 路径` 明确选择该目录下的 SQLite，优先于 DATABASE_URL；路径按当前启动目录解析。`--host` / `--port` 覆盖启动绑定；默认打开浏览器，`--no-browser` 关闭。`start` 会先占用端口、显式升级迁移，再启动；直接运行 ASGI 不自动迁移。

CLI 的 `doctor` / `process` 等命令使用配置中的数据目录，不自动继承上次 start 参数。例如先设置 `PAPERSTEWARD_DATA_DIR=./my-research` 并移除无关 DATABASE_URL，再运行 `papersteward doctor`。避免多个服务同时操作同一目录。

Docker Compose 固定容器数据路径 `/app/var`、本机端口8000、命名卷；根目录 `.env` 中的模型 provider/base_url/model/key 和 Semantic Scholar key 通过显式环境配置传入容器，`.env` 不进入镜像。其他参数需在本地 Compose override 中显式覆盖；不要以为 `.env` 的任意变量都会传入容器。Docker 内的 `127.0.0.1` 指容器本身，本机模型需配置可达的 HTTPS 网关或合适的本地部署；不要绕过非本机 HTTPS 校验。

阅读容量、标签占比、安静时段、暂停及数据授权在 G4 试运行中确认。保存新版评分配置不代表激活；更改全局权重走[预览与确认](advanced/config-governance.md)。

升级前停止服务并复制完整数据目录。Python 安装新版本后再次 start；Docker 重建并使用原卷。迁移失败保留日志并查[排障](troubleshooting.md)，不要删除数据库重试。回到旧版本优先恢复匹配版本的完整副本，降迁移可能删记录，不能作为日常重试方法。
