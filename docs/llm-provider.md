# 真实 LLM provider 配置

支持 OpenAI-compatible `chat/completions`。默认是离线 `FakeLLM`；CLI 显式 `--live`，
或网页 G4 数据授权 + 本次同步授权后，才会发起真实模型请求。配置全部通过 `PAPERSTEWARD_LLM_*` 环境变量或
`.env` 提供，**不要把真实 key 写进仓库**。

## 最小配置

```dotenv
PAPERSTEWARD_LLM_PROVIDER=openai_compatible
PAPERSTEWARD_LLM_BASE_URL=https://api.openai.com/v1
PAPERSTEWARD_LLM_API_KEY=sk-...
PAPERSTEWARD_LLM_MODEL=你的模型名
```

Python/pipx 在启动目录创建 `.env`；Docker 在仓库根目录创建 `.env` 后重新创建服务。
不要把 key 放在命令行参数、截图或网页字段中。配置成功还需完成 G4 数据范围确认，并在
今日页勾选本次授权；默认离线工作区需要新目录重新设置。

然后可选地显式检查连通性（会产生一次很小的真实调用和费用）：

```bash
papersteward llm ping --live
papersteward llm ping --live --json
```

离线检查不需要 key、网络或真实配置：

```bash
papersteward llm ping --fake
```

不传 `--fake`/`--live`、或同时传两个，会直接报错且不发起请求。

## 端点示例

`PAPERSTEWARD_LLM_BASE_URL` 是 **API 根地址**，provider 自动追加
`/chat/completions`。具体模型名以厂商当前文档为准。

| 场景 | BASE_URL | 备注 |
|------|----------|------|
| OpenAI | `https://api.openai.com/v1` | 需要 API key |
| DeepSeek | `https://api.deepseek.com/v1` | 需要 API key；模型如 `deepseek-chat` |
| Ollama（本机） | `http://127.0.0.1:11434/v1` | key 可留空；本机 HTTP 允许 |
| 其他兼容网关 | 以其文档为准 | 非本机必须 HTTPS |

## 全部变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PAPERSTEWARD_LLM_PROVIDER` | `fake` | `fake` / `openai_compatible`；`openai` 为别名 |
| `PAPERSTEWARD_LLM_BASE_URL` | `https://api.openai.com/v1` | API 根地址；不能内嵌认证/查询/片段 |
| `PAPERSTEWARD_LLM_API_KEY` | 空 | 只从环境/.env 读取；空值不发送 Authorization |
| `PAPERSTEWARD_LLM_MODEL` | 空 | `openai_compatible` 时必填 |
| `PAPERSTEWARD_LLM_TIMEOUT_S` | `30` | 单请求硬超时，1–600 秒 |
| `PAPERSTEWARD_LLM_MAX_ATTEMPTS` | `3` | 网关 provider 重试上限，1–5 |
| `PAPERSTEWARD_LLM_PROMPT_COST_PER_1K_USD` | `0` | 仅成本估算 |
| `PAPERSTEWARD_LLM_COMPLETION_COST_PER_1K_USD` | `0` | 仅成本估算 |

成本按响应 `usage.prompt_tokens` / `usage.completion_tokens` 与上述单价计算；未设置
单价时成本为 0，不会伪造账单。真实账单以厂商为准。

## 安全边界

- 默认 `fake`；真实 provider 需要 CLI 显式 `--live`，或网页 G4 数据范围与本次同步授权；
- API key 使用 Pydantic `SecretStr`，不写日志/报告/异常文本；
- 非本机 endpoint 必须 HTTPS；`http://` 只允许 `localhost`/`127.0.0.1`/`::1`；
- 不跟随重定向、不读取代理环境变量；
- 响应超过 4 MiB 拒绝；认证失败/4xx 不重试，429/5xx/超时按网关预算重试；
- prompt 与响应正文不写入运行报告；论文文本始终按不可信数据处理。

`.env` 已被 `.gitignore` 忽略。提交前运行 `make secret-scan`；不要把 key 放在命令
历史、截图、issue 或测试 fixture 中。

## 故障排查

| 现象 | 检查 |
|------|------|
| `PAPERSTEWARD_LLM_MODEL` 报配置错误 | `openai_compatible` 必须设置模型名 |
| `LLM 认证失败` | key 是否正确/过期；是否需要额外账户权限 |
| `LLM 请求失败` | base URL 是否只到 API 根；模型名/路径是否为 `/v1` |
| `LLM 服务不可用` / `限流` | 稍后重试或降低频率；网关最多重试 `MAX_ATTEMPTS` |
| `LLM 请求超时` | 增大 `PAPERSTEWARD_LLM_TIMEOUT_S` 或检查网络 |
| 本机 HTTP 被拒 | 只允许 loopback；其他地址改用 HTTPS |
| `provider=fake` | 显式设置 `PAPERSTEWARD_LLM_PROVIDER=openai_compatible` |

## 当前不做

流式输出、function calling、多个 provider 自动路由、每日成本硬上限、真实模型质量评估。
处理流水线包含本地确定性向量，不表示提供了真实远程 embedding 接口。
