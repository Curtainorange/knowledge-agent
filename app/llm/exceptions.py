"""模型调用异常层级。

- LLMError            所有模型异常基类
- RetryableLLMError   可安全重试（网络抖动、限流、5xx、超时）
- NonRetryableLLMError 不可重试（参数错、鉴权失败、输出校验失败）
"""
from __future__ import annotations


class LLMError(Exception):
    """模型调用异常基类。"""


class RetryableLLMError(LLMError):
    """可重试错误：触发网关重试策略（重试 2 次 + 指数退避 + 抖动）。"""


class NonRetryableLLMError(LLMError):
    """不可重试错误：直接上抛，e.g. 400 参数错误 / 401 鉴权失败。"""