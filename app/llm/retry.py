"""重试策略：重试 2 次 + 指数退避 + 抖动，仅对可重试错误生效（可靠-1）。

由网关调用，避免业务层散落重试逻辑。
"""
from __future__ import annotations

import logging
import random
import time

logger = logging.getLogger(__name__)


def with_retry(
    fn,
    *,
    attempts: int = 3,
    base_delay: float = 0.4,
    max_delay: float = 4.0,
    retry_exceptions: tuple[type[BaseException], ...] = (),
    on_retry: callable | None = None,
) -> object:
    """执行 fn，命中可重试异常时指数退避 + 抖动重试；返回首次成功结果，否则上抛。

    retry_exceptions: 视为可重试的异常类集合，不命中则不重试、直接上抛。
    """
    last_exc: BaseException | None = None
    delay = base_delay
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except retry_exceptions as exc:
            last_exc = exc
            if attempt == attempts:
                break
            jitter = random.uniform(0, delay)
            if on_retry:
                on_retry(attempt, exc)
            time.sleep(delay + jitter)
            delay = min(max_delay, delay * 2)
    assert last_exc is not None
    raise last_exc