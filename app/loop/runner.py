"""Loop 主体：轮次循环 + 停止规则 + 每轮一行报告。

一个 loop 的可靠性不取决于「模型答得好不好」，取决于三件事有没有：
**状态落盘**（崩了能续）、**独立验证**（不认自报的成功）、**停止规则**（不会自己转一夜）。
这三件事分别落在 `state.py` / `verify.py` / 本文件。

token 口径（报告里那个 `tokens=` 与 `¥`）：`cost_logs` 表的 **id 水位线**差分。
每轮开始前记 `MAX(id)`，结束后只统计 `id > 水位线` 且 `task_type='book_digest'` 的行。
比「按时间窗过滤」可靠：同一秒内的多次调用不会漏、不受时区与时钟漂移影响，
还能把同时段别的任务（比如 worker 在跑 L2 扫描）排除在外。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.models.book import Book
from app.domain.models.book_agent_reading import BookAgentReading
from app.domain.models.cost_log import CostLog
from app.loop.state import LoopStore, Task
from app.loop.verify import Verdict, precheck, verify_reading

DIGEST_TASK_TYPE = "book_digest"


@dataclass(frozen=True)
class StopRules:
    """停止规则。默认值偏保守：宁可少读一本，也不要跑掉一夜的 token。

    `max_attempts_per_task` **必须小于** `max_consecutive_failures`，这是刻意的：
    单任务重试耗尽后转 `dead`（不再回队列），熔断只用来拦**跨任务**的系统性问题。
    两者相等会让一本书的 3 次重试正好把熔断打开——那等于「一本书读不了，整个 loop 停摆」，
    熔断器就退化成单任务的重试计数器了。
    """

    max_rounds: int = 5                      # 单次启动最多跑几轮
    max_consecutive_failures: int = 3        # 连续失败这么多轮就熔断，不再领新任务
    max_attempts_per_task: int = 2           # 单任务重试上限，耗尽转 dead（保留现场）


@dataclass
class RoundReport:
    round_no: int
    task_key: str
    title: str
    status: str          # done | partial | failed | dead | error
    elapsed_ms: int
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    cost: float
    verdict: str
    detail: str = ""

    def line(self) -> str:
        """一行报告：任务 / 耗时 / token / 结论。字段固定，方便 grep 与机器解析。"""
        total = self.prompt_tokens + self.completion_tokens
        tail = f" note={self.detail}" if self.detail else ""
        return (
            f"[loop] {self.round_no:02d} task={self.task_key} 《{self.title}》"
            f" status={self.status} {self.elapsed_ms / 1000:.1f}s"
            f" tokens={total}({self.prompt_tokens}+{self.completion_tokens},cache{self.cached_tokens})"
            f" ¥{self.cost:.4f} verdict={self.verdict}{tail}"
        )


@dataclass
class LoopSummary:
    rounds: list[RoundReport] = field(default_factory=list)
    stopped_reason: str = ""
    consecutive_failures: int = 0

    @property
    def ok_rounds(self) -> int:
        return sum(1 for r in self.rounds if r.status in ("done", "partial"))

    def totals(self) -> dict[str, float]:
        return {
            "rounds": len(self.rounds),
            "ok": self.ok_rounds,
            "elapsed_ms": sum(r.elapsed_ms for r in self.rounds),
            "prompt_tokens": sum(r.prompt_tokens for r in self.rounds),
            "completion_tokens": sum(r.completion_tokens for r in self.rounds),
            "cached_tokens": sum(r.cached_tokens for r in self.rounds),
            "cost": round(sum(r.cost for r in self.rounds), 6),
        }


@dataclass(frozen=True)
class TokenDelta:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    cost: float = 0.0


# ------------------------------------------------------------------ 任务来源


@dataclass
class QueueStats:
    queued: int = 0
    skipped: int = 0
    already_done: int = 0


def book_key(book_id: str) -> str:
    return f"book:{book_id}"


def book_id_of(key: str) -> str:
    return key.split(":", 1)[1] if key.startswith("book:") else key


def build_queue(
    store: LoopStore, session: Session, *, book_ids: list[str] | None = None
) -> QueueStats:
    """从书架（books 表）生成任务。**只读**业务库，不写任何业务表。

    三分类：正文不足的书 → skipped（不占轮次、不计失败）；已读完且书没变 → 略过；
    其余 → pending。入队幂等，重复调用不会抹掉已有断点。
    """
    stmt = select(Book).where(Book.is_deleted.is_(False)).order_by(Book.created_at)
    if book_ids:
        stmt = select(Book).where(Book.id.in_(book_ids))
    books = list(session.scalars(stmt))
    stats = QueueStats()

    readings = {
        r.book_id: r for r in session.scalars(
            select(BookAgentReading).where(
                BookAgentReading.book_id.in_([b.id for b in books] or [""])
            )
        )
    }

    for book in books:
        key = book_key(book.id)
        reason = precheck(book)
        if reason:
            stats.skipped += store.enqueue_skipped(key, book.title, reason)
            continue
        reading = readings.get(book.id)
        finished = bool(
            reading
            and not reading.is_deleted
            and reading.status == "done"
            and not reading.truncated
            and reading.total_chars == book.total_chars
        )
        if finished:
            stats.already_done += 1
            continue
        stats.queued += store.enqueue(key, book.title)
    return stats


# ------------------------------------------------------------------ token 口径


def cost_watermark(session: Session) -> int:
    return int(session.scalar(select(func.max(CostLog.id))) or 0)


def tokens_since(session: Session, watermark: int, *, task_type: str | None = DIGEST_TASK_TYPE) -> TokenDelta:
    stmt = select(
        func.coalesce(func.sum(CostLog.prompt_tokens), 0),
        func.coalesce(func.sum(CostLog.completion_tokens), 0),
        func.coalesce(func.sum(CostLog.cached_tokens), 0),
        func.coalesce(func.sum(CostLog.estimated_cost), 0.0),
    ).where(CostLog.id > watermark)
    if task_type:
        stmt = stmt.where(CostLog.task_type == task_type)
    prompt, completion, cached, cost = session.execute(stmt).one()
    return TokenDelta(int(prompt), int(completion), int(cached), float(cost))


# ------------------------------------------------------------------ 主循环


def _default_digest(session, gateway, *, user_id, book, start=None):
    from app.agent.book_orchestrator import read_whole_book

    # force=False：已读完就复用、没读完从断点续读。start 显式传入时优先。
    return read_whole_book(session, gateway, user_id=user_id, book=book, force=False, start=start)


def run_loop(
    store: LoopStore,
    session: Session,
    *,
    gateway=None,
    digest=None,
    rules: StopRules | None = None,
    dry_run: bool = False,
    only_keys: set[str] | None = None,
    log=print,
) -> LoopSummary:
    """跑轮次循环。返回停在哪个原因上（queue_empty / round_limit / circuit_open / dry_run）。

    `only_keys` 限定本轮只处理这些任务（手动重跑某本书时用），不影响队列里其余任务。
    """
    rules = rules or StopRules()
    digest = digest or _default_digest

    if dry_run:
        plan = [t for t in store.pending() if not only_keys or t.key in only_keys]
        for task in plan:
            log(f"[loop] plan task={task.key} 《{task.title}》 attempts={task.attempts}"
                f" read_upto={task.read_upto}")
        if not plan:
            log("[loop] plan （队列为空，先 --rebuild-queue 扫描书架）")
        return LoopSummary([], "dry_run", store.consecutive_failures())

    reclaimed = store.reclaim_running()
    if reclaimed:
        log(f"[loop] 回收上次未正常结束的任务 {reclaimed} 个（断点保留）")

    summary = LoopSummary([], "round_limit", store.consecutive_failures())
    while True:
        if len(summary.rounds) >= rules.max_rounds:
            summary.stopped_reason = "round_limit"
            break
        if store.consecutive_failures() >= rules.max_consecutive_failures:
            # 熔断：不再领新任务。已经烧掉的是沉没成本，继续烧才是灾难。
            summary.stopped_reason = "circuit_open"
            break
        task = store.claim_next(only_keys)
        if task is None:
            summary.stopped_reason = "queue_empty"
            break

        report = _run_one_round(
            store, session, task,
            round_no=len(summary.rounds) + 1,
            gateway=gateway, digest=digest, rules=rules,
        )
        summary.rounds.append(report)
        summary.consecutive_failures = store.consecutive_failures()
        log(report.line())

    return summary


def _run_one_round(
    store: LoopStore, session: Session, task: Task, *,
    round_no: int, gateway, digest, rules: StopRules,
) -> RoundReport:
    book = session.get(Book, book_id_of(task.key))
    if book is None or book.is_deleted:
        # 书不在了也走完整流程（落状态 + 计一次失败），否则队列会卡着一条永远领得到的空任务
        return _settle(
            store, task, round_no,
            verdict=Verdict(False, "hard", ("书不存在或已删除",)),
            status=None, elapsed_ms=0, delta=TokenDelta(), rules=rules,
        )

    watermark = cost_watermark(session)
    started = time.monotonic()
    outcome = None
    try:
        outcome = digest(
            session, gateway,
            user_id=book.user_id, book=book,
            start=task.read_upto if task.read_upto > 0 else None,
        )
        session.commit()
        verdict = verify_reading(session, book.id, user_id=book.user_id)
    except Exception as exc:  # 单轮异常不能打断整个 loop，但必须记成失败
        session.rollback()
        verdict = Verdict(False, "hard", (f"执行异常：{type(exc).__name__}: {exc}",))

    elapsed_ms = int((time.monotonic() - started) * 1000)
    delta = tokens_since(session, watermark)
    return _settle(
        store, task, round_no, verdict=verdict, status=outcome,
        elapsed_ms=elapsed_ms, delta=delta, rules=rules,
    )


def _settle(
    store: LoopStore, task: Task, round_no: int, *,
    verdict: Verdict, status, elapsed_ms: int, delta: TokenDelta, rules: StopRules,
) -> RoundReport:
    """根据验证结论落任务状态、写轮次记录、生成一行报告。"""
    truncated = bool(getattr(status, "truncated", False))
    chunk_count = int(getattr(status, "chunk_count", 0) or 0)

    if verdict.ok:
        # 只要这一轮验证通过就清零：否则一本超长书连读 3 次 truncated 会被误熔断
        store.set_consecutive_failures(0)
        if truncated:
            store.release_continue(task.key, read_upto=chunk_count, note="truncated，待续读")
            report_status = "partial"
        else:
            store.finish(task.key, "done", read_upto=chunk_count or task.read_upto)
            report_status = "done"
    else:
        store.set_consecutive_failures(store.consecutive_failures() + 1)
        if task.attempts >= rules.max_attempts_per_task:
            store.finish(task.key, "dead", error=verdict.line())
            report_status = "dead"
        else:
            store.release_continue(task.key, read_upto=task.read_upto, note=verdict.line())
            report_status = "failed"

    report = RoundReport(
        round_no=round_no, task_key=task.key, title=task.title, status=report_status,
        elapsed_ms=elapsed_ms, prompt_tokens=delta.prompt_tokens,
        completion_tokens=delta.completion_tokens, cached_tokens=delta.cached_tokens,
        cost=delta.cost, verdict=verdict.level, detail=verdict.line(),
    )
    store.record_round(
        round_no=report.round_no, task_key=report.task_key, status=report.status,
        elapsed_ms=report.elapsed_ms, prompt_tokens=report.prompt_tokens,
        completion_tokens=report.completion_tokens, cached_tokens=report.cached_tokens,
        cost=report.cost, verdict=report.verdict, detail=report.detail,
    )
    return report
