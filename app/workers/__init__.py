"""异步主动链路（worker）——单进程守护线程 + 落库任务队列。

任务框架见 `tasks.py`（幂等入队 / 领取执行 / 重试 / 死信 / stale 回收），
处理器注册见 `handlers.py`，周期任务的幂等键排程见 `triggers.py`。
"""
from __future__ import annotations

__all__: list[str] = []
