from papersteward.jobs.ui_flow import llm_failure_message
from papersteward.llm.errors import LLMProviderError, LLMValidationError


def test_auth_failure_points_to_settings_test_connection() -> None:
    message = llm_failure_message(LLMProviderError("LLM 认证失败", retryable=False))
    assert "认证失败" in message
    assert "设置" in message and "测试连接" in message


def test_schema_failure_mentions_retry_or_switch_model() -> None:
    message = llm_failure_message(LLMValidationError("schema.x: 缺少字段", field="schema.x"))
    assert "模型返回不符合要求" in message
    assert "重试" in message


def test_timeout_suggests_increasing_timeout() -> None:
    message = llm_failure_message(LLMProviderError("LLM 请求超时"))
    assert "超时" in message and "设置" in message
