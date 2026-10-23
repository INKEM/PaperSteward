# 模型接入

PaperSteward 通过 OpenAI-compatible `chat/completions` 调用模型。**普通用户只需要浏览器设置页**：填入 API 根地址、密钥和模型名，测试连接，保存即可。配置进入本机工作区数据库，保存即授权数据外发，无需 `.env`，无需重启。

## 在设置页配置

打开 `/ui/settings`（首次设置第一步也有同一表单）：

1. **API 地址**：只填到 API 根，provider 会自动追加 `/chat/completions`。例如 OpenAI 填 `https://api.openai.com/v1`，不要填完整的 `/chat/completions` 路径。
2. **API 密钥**：本机模型可留空；非本机地址必填。留空保存时沿用上一次保存的密钥；若 `.env` 中同 origin 有密钥，会沿用并标注来源。远端仍无密钥时会拒绝保存，而不是发出无认证请求。
3. **模型名**：以厂商当前文档为准，例如 `deepseek-chat`、`gpt-4o-mini`。
4. 勾选知情同意并保存。论文标题与摘要会发送给该模型服务，用于分类、概括、评分和初始研究档案生成；阅读备注不外发。

**测试连接** 会用表单当前值发起一次很小的真实补全请求，不落库。失败时页面显示可操作原因（地址、认证、限流、网络等），不会回显密钥。

保存后设置页只展示密钥掩码（如 `****1234`）。配置以版本化记录追加，并写入一条不含密钥的审计事件；旧版本仍然保留，便于排查。

## 常用端点

`BASE_URL` 是 **API 根地址**，不是单个接口地址。

| 场景 | BASE_URL | 备注 |
|------|----------|------|
| OpenAI | `https://api.openai.com/v1` | 需要 API key |
| DeepSeek | `https://api.deepseek.com/v1` | 需要 API key；模型如 `deepseek-chat` |
| Ollama（本机） | `http://127.0.0.1:11434/v1` | key 可留空；本机 HTTP 允许 |
| 其他兼容网关 | 以其文档为准 | 非本机必须 HTTPS |

## 环境变量与 CLI（高级 / 容器）

数据库没有 `llm_config` 时，系统回退到 `PAPERSTEWARD_LLM_*` 环境变量 / `.env`。这条路径保留原有 CLI 语义：真实调用必须显式 `--live`，网页同步必须先在设置页保存一次配置以获得授权。

```dotenv
PAPERSTEWARD_LLM_PROVIDER=openai_compatible
PAPERSTEWARD_LLM_BASE_URL=https://api.openai.com/v1
PAPERSTEWARD_LLM_API_KEY=sk-...
PAPERSTEWARD_LLM_MODEL=你的模型名
```

可选地显式检查连通性（会产生一次很小的真实调用和费用）：

```bash
papersteward llm ping --live
papersteward llm ping --live --json
```

`--fake` 仅用于离线开发与契约测试，不是用户可见的"离线模式"，也不代表真实模型质量。

成本按响应 `usage.prompt_tokens` / `usage.completion_tokens` 与成本单价计算；未设置单价时成本为 0，不会伪造账单。真实账单以厂商为准。

## 安全边界

- 数据库配置自带授权；环境变量路径需要 CLI `--live` 或网页保存一次设置；
- API key 使用 Pydantic `SecretStr`，不写日志/报告/异常文本，页面只显示掩码；
- 非本机 endpoint 必须 HTTPS；`http://` 只允许 `localhost`/`127.0.0.1`/`::1`；
- 不跟随重定向、不读取代理环境变量；
- 响应超过 4 MiB 拒绝；认证失败/4xx 不重试，429/5xx/超时按网关预算重试；
- prompt 与响应正文不写入运行报告；论文文本始终按不可信数据处理；
- 网关对 schema 校验失败会自动修复重试一次；仍失败时运行页会显示具体字段错误，不会静默采用坏数据。

`.env` 已被 `.gitignore` 忽略。提交前运行 `make secret-scan`；不要把 key 放在命令历史、截图、issue 或测试 fixture 中。

## 故障排查

| 现象 | 检查 |
|------|------|
| 设置页提示未配置 | 保存一次模型配置；数据库配置优先于环境变量 |
| 同步时报 `LLM 认证失败` | API 密钥为空或无效。到设置页填入密钥，点「测试连接」确认后再同步 |
| `LLM 认证失败` | key 是否正确/过期；是否需要额外账户权限 |
| `LLM 请求失败` | base URL 是否只到 API 根；模型名/路径是否为 `/v1` |
| `LLM 服务不可用` / `限流` | 稍后重试或降低频率；网关最多重试 `MAX_ATTEMPTS` |
| `LLM 请求超时` | 增大超时时间或检查网络 |
| 本机 HTTP 被拒 | 只允许 loopback；其他地址改用 HTTPS |
| 生成草案报 schema 字段错误 | 模型输出不稳定；重试，或换一个更强的模型 |

## 当前不做

流式输出、function calling、多个 provider 自动路由、每日成本硬上限、真实模型质量评估。处理流水线包含本地确定性向量，不表示提供了真实远程 embedding 接口。
