"""结构化日志：统一在每条记录上注入 request_id / task_id 字段。

最小化日志原则（需求安全-2 / 安全-5）：业务日志禁止记录正文、知识源凭证、
完整提示词；必要字段脱敏。日志仅记录元数据与长度计数。
"""
from __future__ import annotations

import io
import logging
import sys

from app.core import trace


class RequestIdFilter(logging.Filter):
    """给每条日志记录附加 request_id / task_id 字段。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = trace.get_request_id() or "-"
        record.task_id = trace.get_task_id() or "-"
        return True


def setup_logging() -> None:
    # 强制 UTF-8 输出，规避中文 Windows 控制台 GBK 码页导致的日志乱码
    handler = logging.StreamHandler(io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8"))
    handler.addFilter(RequestIdFilter())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s [req=%(request_id)s task=%(task_id)s] %(message)s",
        handlers=[handler],
        force=True,
    )