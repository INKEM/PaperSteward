# ADR 0013：真实 LLM 使用 OpenAI-compatible HTTP provider 与显式授权

- **状态**：接受
- **日期**：2026-10-22
- **阶段**：P4-S1

## 背景

P1-S5 只交付了 `LLMProvider` 协议、`FakeLLM`、网关重试/schema 修复与成本记录，明确
不访问真实网络。P4 的 R、摘要、分类需要真实模型；用户要求提供可配置的真实 LLM
API 入口。默认假设（大阶段计划 §0）是 OpenAI-compatible LLM。

## 决策

1. 用已有运行时依赖 **httpx** 实现最小 OpenAI-compatible provider：
   `POST <base_url>/chat/completions`，不引入 LiteLLM、OpenAI SDK 或厂商 SDK。
2. 配置全部走 `PAPERSTEWARD_LLM_*` 环境变量；`PAPERSTEWARD_LLM_API_KEY` 只以
   `SecretStr` 保存并在请求头使用，不写日志、报告或异常文本。
3. 默认 `llm_provider=fake`；真实 provider 只有调用方显式传 `live=True`
   （CLI 为 `papersteward llm ping --live`）才构建，避免隐式联网/费用。
4. provider 只做一次补全；重试、schema 校验、一次修复由现有 `LLMGateway` 负责。
   provider 标记 `retryable=False` 的认证/请求错误不被网关重试。
5. 非本机地址必须 HTTPS；禁止 URL 内嵌认证、查询或片段；客户端不跟随重定向、
   不读取代理环境；硬超时由 `httpx` per-request timeout 实现；响应上限 4 MiB。
6. 测试只用 `FakeLLM`/`httpx.MockTransport`；真实调用不进入 CI；live 测试仍手动。

## 替代方案

| 方案 | 不采用原因 |
|------|------------|
| LiteLLM | 新依赖面大，代理/路由能力超出 P4-S1；安全边界不透明 |
| OpenAI 官方 SDK | 新依赖，且只覆盖 OpenAI；本地/Ollama 兼容性需另做适配 |
| 各厂商 SDK | 多套认证/错误模型，和现有 provider 协议重复 |
| 继续只用 FakeLLM | 无法完成 P4 真实摘要/分类；用户明确要求真实入口 |

## 影响

- 无新依赖、无迁移；配置新增 8 个变量，默认值保持离线安全；
- 真实调用的费用/隐私风险由显式 `--live` 与 `.env` 不提交控制；
- `llm ping --fake` 不受真实配置缺失影响，可在无密钥环境自检；
- 回滚：删除 provider/factory/配置字段与 CLI `--live` 分支；FakeLLM/网关契约不变。

## 约束

- 不实现流式输出、function calling、embedding 或厂商专属参数；
- 不把模型返回的 prompt/响应原文写入日志；
- API key 缺失允许本机无认证端点（如 Ollama），远程端点由用户自行配置；
- 真实模型质量、价格与限流行为不在本 ADR 承诺范围内。
