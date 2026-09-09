"""能力层（L1~L5）—— P0 仅建边界，不实现具体能力。

铁律（架构要求）：五大能力独立模块，互不 import；全部模型调用经 llm 网关；
跨能力写操作经 domain 仓储。L2~L5 在 P1 起逐个实现。
"""
from __future__ import annotations

# 占位：避免 IDE 误判为空包
__all__: list[str] = []