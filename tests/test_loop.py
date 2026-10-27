"""Loop 工程三件套（状态队列 / 独立验证器 / 停止规则）的测试。

这里全部用假 digest 注入，不调模型、不碰真实库：
- `LoopStore` 用 tmp_path 下的独立 sqlite 文件，验证「断点可续」靠的是状态而非内存变量；
- `verify_reading` 直接从 ORM 读回来验，不信任编排层自报的 `state`；
- 停止规则用「第几次失败」驱动，断言熔断确实在连续第 3 次失败后打开。
"""
from __future__ import annotations

import pytest

from app.domain.models.book import Book
from app.domain.models.book_agent_reading import BookAgentReading
from app.loop.runner import RoundReport, StopRules, run_loop
from app.loop.state import LoopStore
from app.loop.verify import MIN_READABLE_CHARS, precheck, verify_reading

LONG_TEXT = "认知负荷与刻意练习的关系，是本段用来凑够正文长度的说明性文字。" * 200


def _make_book(session, *, title="测试书", text=LONG_TEXT, user_id="u1") -> Book:
    book = Book(
        user_id=user_id,
        title=title,
        format="txt",
        file_path="",
        full_text=text,
        total_chars=len(text),
        chapters=[{"index": 0, "title": "第一章", "char_start": 0, "char_end": len(text)}],
    )
    session.add(book)
    session.commit()
    return book


def _make_reading(
    session,
    book,
    *,
    indexes=(0, 1, 2),
    summary="全书总评：作者用认知负荷与刻意练习两条线索，说明为什么有效练习必须带反馈，并给出可执行的练习设计。",
    failed_chunks=0,
    status="done",
    truncated=False,
) -> BookAgentReading:
    reading = BookAgentReading(
        user_id=book.user_id,
        book_id=book.id,
        status=status,
        total_chars=book.total_chars,
        summary=summary,
        chapters_note=[
            {"index": i, "title": f"块{i}", "topic": "主题", "gist": "要点", "points": ["p"]}
            for i in indexes
        ],
        chunk_count=(max(indexes) + 1) if indexes else 0,
        failed_chunks=failed_chunks,
        truncated=truncated,
    )
    session.add(reading)
    session.commit()
    return reading


# ---------------------------------------------------------------- 状态队列


def test_claim_is_exclusive(tmp_path):
    """两个 worker 不会领到同一条任务（条件 UPDATE 领取）。"""
    store = LoopStore(tmp_path / "s.db")
    store.enqueue("book:a", "书A")
    store.enqueue("book:b", "书B")

    first = store.claim_next()
    second = store.claim_next()
    third = store.claim_next()

    assert {first.key, second.key} == {"book:a", "book:b"}
    assert third is None


def test_running_task_is_reclaimed_after_crash(tmp_path):
    """进程崩在 running 上，重开一个 store 必须能把它捡回来——否则任务永久卡死。"""
    path = tmp_path / "s.db"
    store = LoopStore(path)
    store.enqueue("book:a", "书A")
    store.claim_next()

    reopened = LoopStore(path)
    assert reopened.claim_next() is None  # 还没回收时领不到
    assert reopened.reclaim_running() == 1
    task = reopened.claim_next()
    assert task is not None and task.key == "book:a"
    assert task.attempts == 2  # 领取时 +1，回收不改计数；再领一次就是 2 —— 毒任务不会无限回收


def test_checkpoint_survives_release(tmp_path):
    """没读完的任务放回队列时必须带上已读块数，下一轮才能接着读。"""
    store = LoopStore(tmp_path / "s.db")
    store.enqueue("book:a", "书A")
    store.claim_next()
    store.release_continue("book:a", read_upto=7, note="truncated")

    task = store.claim_next()
    assert task.read_upto == 7


def test_enqueue_is_idempotent(tmp_path):
    store = LoopStore(tmp_path / "s.db")
    store.enqueue("book:a", "书A")
    store.enqueue("book:a", "书A")
    assert len(store.pending()) == 1


# ---------------------------------------------------------------- 独立验证器


def test_verify_passes_well_formed_reading(session):
    book = _make_book(session)
    _make_reading(session, book)

    verdict = verify_reading(session, book.id)

    assert verdict.ok and verdict.level == "pass", verdict.reasons


def test_verify_hard_fails_without_reading(session):
    """扫描版书编排层返回 degraded、一条记录都不落——验证器必须判失败，而不是「没报错就算过」。"""
    book = _make_book(session)
    verdict = verify_reading(session, book.id)
    assert not verdict.ok and verdict.level == "hard"
    assert any("没有通读记录" in r for r in verdict.reasons)


def test_verify_hard_fails_on_chunk_hole(session):
    """块序号有洞（0,2 缺 1）说明断点合并出过问题，读出来的要点不完整。"""
    book = _make_book(session)
    _make_reading(session, book, indexes=(0, 2))

    verdict = verify_reading(session, book.id)

    assert not verdict.ok and verdict.level == "hard"
    assert any("空洞" in r for r in verdict.reasons)


def test_verify_soft_fails_on_failed_chunks(session):
    """部分块失败不算失败（其余部分可用），但要留下 soft 告警。"""
    book = _make_book(session)
    _make_reading(session, book, failed_chunks=1)

    verdict = verify_reading(session, book.id)

    assert verdict.ok and verdict.level == "soft"


def test_verify_hard_fails_on_metadata_only_summary(session):
    """整本 27 字的「插图本」能跑出 done，但正文不够，验证器要拦住。"""
    book = _make_book(session, text="插图本")
    _make_reading(session, book, indexes=(0,))

    verdict = verify_reading(session, book.id)

    assert not verdict.ok and verdict.level == "hard"
    assert any("正文过短" in r for r in verdict.reasons)


def test_precheck_skips_unreadable_book(session):
    """排队阶段就把没有正文的书挑出来——这是人原来手动做的那一步「这本书值不值得读」。"""
    book = _make_book(session, text="插图本")
    reason = precheck(book)
    assert reason and "正文过短" in reason

    good = _make_book(session, title="正常书")
    assert precheck(good) is None
    assert len(LONG_TEXT) > MIN_READABLE_CHARS


# ---------------------------------------------------------------- 停止规则与报告


class _Outcome:
    """假通读结果，字段与 DigestOutcome 对齐（只取 runner 用到的）。"""

    def __init__(self, *, chunk_count=3, truncated=False, state="ok", note=""):
        self.chunk_count = chunk_count
        self.truncated = truncated
        self.state = state
        self.failed_chunks = 0
        self.note = note


def _store_with(tmp_path, keys):
    store = LoopStore(tmp_path / "s.db")
    for i, key in enumerate(keys):
        store.enqueue(key, f"书{i}")
    return store


def test_round_limit_stops_the_loop(session, tmp_path):
    """轮次上限必须硬停——否则队列里剩多少本就要烧多少 token。"""
    books = [_make_book(session, title=f"书{i}") for i in range(5)]
    store = _store_with(tmp_path, [f"book:{b.id}" for b in books])
    calls = []

    def digest(sess, gateway, *, user_id, book, start=None):
        calls.append(book.id)
        _make_reading(sess, book)
        return _Outcome()

    summary = run_loop(
        store, session, gateway=None, digest=digest,
        rules=StopRules(max_rounds=2, max_consecutive_failures=3),
        log=lambda _line: None,
    )

    assert len(calls) == 2
    assert summary.stopped_reason == "round_limit"
    assert len(summary.rounds) == 2


def test_circuit_opens_after_three_consecutive_failures(session, tmp_path):
    """连续 3 轮失败就熔断：不再领新任务，返回 circuit_open。"""
    books = [_make_book(session, title=f"坏书{i}") for i in range(5)]
    store = _store_with(tmp_path, [f"book:{b.id}" for b in books])
    calls = []

    def digest(sess, gateway, *, user_id, book, start=None):
        calls.append(book.id)
        return _Outcome(state="degraded")  # 不落记录 → 验证器判 hard fail

    summary = run_loop(
        store, session, gateway=None, digest=digest,
        # 关掉重试（每本只试一次），否则失败的书会一直回到队列占掉后续轮次，
        # 这个用例就测不到「成功清零失败计数」这件事了
        rules=StopRules(max_rounds=10, max_consecutive_failures=3, max_attempts_per_task=1),
        log=lambda _line: None,
    )

    assert len(calls) == 3
    assert summary.stopped_reason == "circuit_open"
    assert summary.consecutive_failures == 3


def test_single_book_retries_to_dead_without_opening_circuit(session, tmp_path):
    """一本书连续失败只该把自己耗成 dead，不该把整个 loop 熔断。

    默认参数（重试 2 次 < 熔断 3 次）就是为这条服务的：否则一本扫描版书
    就能让整个队列停摆。
    """
    book = _make_book(session)
    store = _store_with(tmp_path, [f"book:{book.id}"])

    summary = run_loop(
        store, session, gateway=None,
        digest=lambda *a, **k: _Outcome(state="degraded"),
        rules=StopRules(max_rounds=5),
        log=lambda _line: None,
    )

    assert [r.status for r in summary.rounds] == ["failed", "dead"]
    assert summary.stopped_reason == "queue_empty"
    assert summary.consecutive_failures == 2


def test_success_resets_the_failure_streak(session, tmp_path):
    """成功一次就要清零，否则隔三差五失败一次也会被误熔断。"""
    books = [_make_book(session, title=f"书{i}") for i in range(6)]
    store = _store_with(tmp_path, [f"book:{b.id}" for b in books])
    plan = ["fail", "fail", "ok", "fail", "fail", "ok"]

    def digest(sess, gateway, *, user_id, book, start=None):
        verdict_mode = plan[len([r for r in calls])]
        calls.append(1)
        if verdict_mode == "ok":
            _make_reading(sess, book)
        return _Outcome(state="ok" if verdict_mode == "ok" else "degraded")

    calls: list[int] = []
    summary = run_loop(
        store, session, gateway=None, digest=digest,
        # 7 轮上限 > 6 个任务，且关掉重试（每本只试一次）：让循环跑到「队列空」停下，
        # 才能证明失败计数被成功那轮清零了（没清零的话第 2 次失败就把熔断打开了）
        rules=StopRules(max_rounds=7, max_consecutive_failures=3, max_attempts_per_task=1),
        log=lambda _line: None,
    )

    assert summary.stopped_reason == "queue_empty"
    # 每本只试一次，所以失败的直接是 dead（不是 failed 待重试）
    assert [r.status for r in summary.rounds] == ["dead", "dead", "done", "dead", "dead", "done"]


def test_truncated_task_is_requeued_with_checkpoint(session, tmp_path):
    """单次只读 N 块：没读完要放回队列并带断点，下一轮从断点续读。"""
    book = _make_book(session)
    store = _store_with(tmp_path, [f"book:{book.id}"])
    starts = []

    def digest(sess, gateway, *, user_id, book, start=None):
        starts.append(start)
        _make_reading(sess, book)
        if len(starts) == 1:
            return _Outcome(chunk_count=4, truncated=True)
        return _Outcome(chunk_count=8, truncated=False)

    summary = run_loop(
        store, session, gateway=None, digest=digest,
        rules=StopRules(max_rounds=5, max_consecutive_failures=3),
        log=lambda _line: None,
    )

    assert starts == [None, 4], "第二轮必须从断点 4 续读"
    assert summary.rounds[-1].status == "done"
    assert summary.stopped_reason == "queue_empty"


def test_report_line_carries_task_elapsed_tokens_and_verdict():
    """一行报告的四个字段：任务、耗时、token、结论。少一个就不叫报告。"""
    report = RoundReport(
        round_no=3, task_key="book:abc", title="把时间当作朋友", status="done",
        elapsed_ms=1234, prompt_tokens=1200, completion_tokens=300,
        cached_tokens=0, cost=0.0012, verdict="pass", detail="",
    )
    line = report.line()
    assert "book:abc" in line and "把时间当作朋友" in line
    assert "1.2s" in line
    assert "tokens=1500" in line
    assert "verdict=pass" in line
    assert "\n" not in line


def test_dry_run_does_not_consume_tasks(session, tmp_path):
    """dry-run 只报计划，不领任务、不写状态——否则「先看一眼」会真的把队列走掉。"""
    book = _make_book(session)
    store = _store_with(tmp_path, [f"book:{book.id}"])

    summary = run_loop(
        store, session, gateway=None, digest=lambda *a, **k: pytest.fail("dry-run 不该调用 digest"),
        rules=StopRules(max_rounds=3), dry_run=True, log=lambda _line: None,
    )

    assert summary.stopped_reason == "dry_run"
    assert len(store.pending()) == 1


def test_only_keys_never_claims_other_tasks(session, tmp_path):
    """手动重跑某本书时，队列里更早的 pending 任务不能被顺手领走。

    这是实测踩到的：`--book-id A` 跑完，动的却是队列里排在 A 前面的 B——
    状态库看着正常，但「我让你跑这本你跑了那本」最伤信任。
    """
    books = [_make_book(session, title=f"书{i}") for i in range(2)]
    store = _store_with(tmp_path, [f"book:{b.id}" for b in books])
    target = f"book:{books[1].id}"
    calls: list[str] = []

    def digest(sess, gateway, *, user_id, book, start=None):
        calls.append(book.id)
        _make_reading(sess, book)
        return _Outcome()

    summary = run_loop(
        store, session, gateway=None, digest=digest,
        rules=StopRules(max_rounds=3), only_keys={target}, log=lambda _line: None,
    )

    assert calls == [books[1].id]
    assert summary.stopped_reason == "queue_empty"
    assert len(store.pending()) == 1  # 另一本原封不动留在队列里


# ---------------------------------------------------------------- token 口径

def test_digest_spend_is_recorded_in_cost_logs(session):
    """通读的 token 必须落 cost_logs，否则 loop 报告里的 tokens 永远是 0。

    这条曾经是真的 0：`gateway.chat()` 的 session 默认 None，编排层三处调用都没传，
    于是 token 只进了日志、没进库——成本查不到账，而且「没报错」所以没人发现。
    """
    from sqlalchemy import func, select

    from app.agent.book_orchestrator import read_whole_book
    from app.domain.models.cost_log import CostLog
    from app.llm.gateway import ModelGateway

    book = _make_book(session)
    read_whole_book(session, ModelGateway(), user_id=book.user_id, book=book)
    session.commit()

    rows = session.scalar(
        select(func.count()).select_from(CostLog).where(CostLog.task_type == "book_digest")
    )
    tokens = session.scalar(
        select(func.sum(CostLog.prompt_tokens)).where(CostLog.task_type == "book_digest")
    )
    assert rows and rows >= 1
    assert tokens and tokens > 0

