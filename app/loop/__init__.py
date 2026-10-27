"""Loop 工程三件套：状态队列（断点可续）/ 独立验证器 / 停止规则 + 每轮一行报告。

入口在 `scripts/run_loop.py`，职责划分：
- `state.LoopStore` —— 任务与断点落盘，崩了能捡回来；
- `verify.verify_reading` —— 只认库里的事实，不认编排层自报的成功；
- `runner.run_loop` —— 轮次循环、熔断、token 口径与报告行。
"""
from app.loop.runner import (
    LoopSummary,
    QueueStats,
    RoundReport,
    StopRules,
    build_queue,
    run_loop,
)
from app.loop.state import LoopStore, Task
from app.loop.verify import Verdict, precheck, verify_reading

__all__ = [
    "LoopStore", "Task",
    "Verdict", "precheck", "verify_reading",
    "StopRules", "RoundReport", "LoopSummary", "QueueStats", "run_loop", "build_queue",
]
