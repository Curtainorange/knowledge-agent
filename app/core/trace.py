"""request_id / task_id 链路追踪。

用 contextvar 承载，贯穿「API 中间件 → agent → 网关 → provider」，
保证一次用户请求的日志、成本、审计可全链路归因到同一条 request_id。
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator, Optional

_request_id: ContextVar[Optional[str]] = ContextVar("request_id", default=None)
_task_id: ContextVar[Optional[str]] = ContextVar("task_id", default=None)


@contextmanager
def request_id(value: str) -> Iterator[None]:
    """进入一段标注了 request_id 的作用域（中间件/网关调用边界）。"""
    token: Token = _request_id.set(value)
    try:
        yield
    finally:
        _request_id.reset(token)


@contextmanager
def task_id(value: str) -> Iterator[None]:
    """标注异步任务的 task_id（worker 链路用，P0 预留）。"""
    token: Token = _task_id.set(value)
    try:
        yield
    finally:
        _task_id.reset(token)


def get_request_id() -> Optional[str]:
    return _request_id.get()


def get_task_id() -> Optional[str]:
    return _task_id.get()