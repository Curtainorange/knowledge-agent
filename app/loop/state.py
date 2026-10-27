"""Loop 状态队列：独立 SQLite 文件，任务状态与断点都落盘。

为什么不用 `app/workers/tasks.py` 那套表：那是**业务**任务队列（跑在 dev.db 里，
服务重启后 worker 接着跑）。这里是**工具层**的 loop，任务对象是「书」，
运行时不该碰业务库的表结构——所以状态单独一个库文件（`runtime/loop_state.db`，
被 `*.db` 规则忽略），删掉即重置，不产生迁移、不影响生产数据。

断点可续靠三件事：
1. `running` 状态会在下一次启动时被 `reclaim_running()` 捡回 `pending`；
2. `attempts` 在**领取时**就 +1，进程崩在任务中间也算一次尝试——毒任务不会无限回收；
3. `read_upto`（已消费块数）随任务持久化，重跑时作为 `start` 传给编排层。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    key         TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    attempts    INTEGER NOT NULL DEFAULT 0,
    read_upto   INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, created_at);

CREATE TABLE IF NOT EXISTS rounds (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    round_no           INTEGER NOT NULL,
    task_key           TEXT NOT NULL,
    status             TEXT NOT NULL,
    started_at         TEXT NOT NULL,
    elapsed_ms         INTEGER NOT NULL,
    prompt_tokens      INTEGER NOT NULL,
    completion_tokens  INTEGER NOT NULL,
    cached_tokens      INTEGER NOT NULL,
    cost               REAL NOT NULL,
    verdict            TEXT NOT NULL,
    detail             TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS control (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Task:
    key: str
    title: str
    status: str
    attempts: int
    read_upto: int
    last_error: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class LoopStore:
    """任务状态机：pending → running → done / failed(回 pending) / dead / skipped。

    `failed` 不落库：失败的任务回到 `pending` 等下一轮重试，`attempts` 记在任务上；
    只有 attempts 耗尽才转 `dead`（保留现场，不再自动重试）。
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------ 入队

    def enqueue(self, key: str, title: str) -> bool:
        """幂等入队。已存在的任务（任何状态）都不会被覆盖——重跑不该抹掉断点。"""
        now = _now()
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO tasks(key, title, status, created_at, updated_at)"
                " VALUES (?, ?, 'pending', ?, ?)",
                (key, title, now, now),
            )
            return cur.rowcount == 1

    def enqueue_skipped(self, key: str, title: str, reason: str) -> bool:
        """预检不合格（正文过短 / 扫描版占位）直接记 `skipped`，不占轮次、不算失败。"""
        now = _now()
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO tasks(key, title, status, last_error, created_at, updated_at)"
                " VALUES (?, ?, 'skipped', ?, ?, ?)",
                (key, title, f"预检跳过：{reason}", now, now),
            )
            return cur.rowcount == 1

    # ------------------------------------------------------------ 领取 / 回收

    def pending(self) -> list[Task]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status = 'pending' ORDER BY created_at, key"
            ).fetchall()
        return [_to_task(r) for r in rows]

    def claim_next(self, only_keys: set[str] | None = None) -> Task | None:
        """条件 UPDATE 领取：只有把 pending 改成 running 成功的那一方拿到任务。

        `only_keys` 用于「只跑指定的几本书」——不带过滤时会领走队列里最早的 pending，
        手动重跑某本却跑了另一本，是最容易让人怀疑状态库坏了的一种 bug。
        """
        where = "status = 'pending'"
        params: list[str] = []
        if only_keys:
            where += f" AND key IN ({','.join('?' * len(only_keys))})"
            params = sorted(only_keys)
        for _ in range(5):
            with self._conn() as conn:
                row = conn.execute(
                    f"SELECT key FROM tasks WHERE {where} ORDER BY created_at, key LIMIT 1",
                    params,
                ).fetchone()
                if row is None:
                    return None
                cur = conn.execute(
                    "UPDATE tasks SET status = 'running', attempts = attempts + 1, updated_at = ?"
                    " WHERE key = ? AND status = 'pending'",
                    (_now(), row["key"]),
                )
                if cur.rowcount == 1:
                    return _to_task(
                        conn.execute("SELECT * FROM tasks WHERE key = ?", (row["key"],)).fetchone()
                    )
        return None

    def reclaim_running(self) -> int:
        """把上次崩在 `running` 的任务捡回 `pending`（attempts 已在领取时计过，这里不加）。"""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE tasks SET status = 'pending', last_error = '上次未正常结束，已回收',"
                " updated_at = ? WHERE status = 'running'",
                (_now(),),
            )
            return cur.rowcount

    # ------------------------------------------------------------ 落状态

    def finish(self, key: str, status: str, *, read_upto: int | None = None, error: str = "") -> None:
        with self._conn() as conn:
            if read_upto is None:
                conn.execute(
                    "UPDATE tasks SET status = ?, last_error = ?, updated_at = ? WHERE key = ?",
                    (status, error, _now(), key),
                )
            else:
                conn.execute(
                    "UPDATE tasks SET status = ?, read_upto = ?, last_error = ?, updated_at = ?"
                    " WHERE key = ?",
                    (status, read_upto, error, _now(), key),
                )

    def release_continue(self, key: str, *, read_upto: int, note: str = "") -> None:
        """放回队列并保存断点（没读完 / 本轮失败待重试）。"""
        with self._conn() as conn:
            conn.execute(
                "UPDATE tasks SET status = 'pending', read_upto = ?, last_error = ?, updated_at = ?"
                " WHERE key = ?",
                (read_upto, note, _now(), key),
            )

    def reset_task(self, key: str) -> bool:
        """手动重跑：清零尝试次数与断点，回到 pending。"""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE tasks SET status = 'pending', attempts = 0, read_upto = 0,"
                " last_error = '', updated_at = ? WHERE key = ?",
                (_now(), key),
            )
            return cur.rowcount == 1

    # ------------------------------------------------------------ 熔断计数

    def consecutive_failures(self) -> int:
        with self._conn() as conn:
            row = conn.execute("SELECT v FROM control WHERE k = 'consecutive_failures'").fetchone()
        return int(row["v"]) if row else 0

    def set_consecutive_failures(self, value: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO control(k, v) VALUES ('consecutive_failures', ?)"
                " ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                (str(int(value)),),
            )

    # ------------------------------------------------------------ 轮次记录

    def record_round(
        self, *, round_no: int, task_key: str, status: str, elapsed_ms: int,
        prompt_tokens: int, completion_tokens: int, cached_tokens: int,
        cost: float, verdict: str, detail: str = "",
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO rounds(round_no, task_key, status, started_at, elapsed_ms,"
                " prompt_tokens, completion_tokens, cached_tokens, cost, verdict, detail)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (round_no, task_key, status, _now(), elapsed_ms, prompt_tokens,
                 completion_tokens, cached_tokens, cost, verdict, detail),
            )

    def stats(self) -> dict[str, int]:
        with self._conn() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}


def _to_task(row: sqlite3.Row) -> Task:
    return Task(
        key=row["key"], title=row["title"], status=row["status"],
        attempts=row["attempts"], read_upto=row["read_upto"], last_error=row["last_error"],
    )
