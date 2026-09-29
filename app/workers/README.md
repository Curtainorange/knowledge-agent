# 异步主动链路（worker）

单进程守护线程 + 落库任务队列（不依赖 Celery/Redis/APScheduler）。

- `runner.py`：worker 主循环（轮询 `run_once`，`nudge()` 可提前唤醒）
- `tasks.py`：任务框架——幂等入队（`idempotency_key` 唯一约束）、领取执行、
  退避重试、死信、stale-running 回收（崩溃遗留的 `running` 超时回 `pending`）
- `handlers.py`：处理器注册（`l2_scan` / `agent_turn` / `push_weekly_digest` /
  `push_monthly_health` 等）
- `triggers.py`：周期任务排程——「幂等键表达周期」（`l2:weekly:{user}:{ISO周}` 等），
  worker 每轮 ensure 本周期 key 存在即可，同周期天然只跑一次
