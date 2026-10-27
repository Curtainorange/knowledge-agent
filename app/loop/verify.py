"""独立验证器：只认库里的事实，不认编排层自报的 `state`。

「独立」是这条流程里最容易被做假的一环：编排层返回 `state="ok"` 只说明它没抛异常，
不说明**读出来的东西是可用的**。历史上就吃过这个亏——扫描图片版 epub 能「成功」
跑出一份只有封面占位符与出版信息的摘要（status=done），看着像读过，实际一个字没读到。

所以这里：
- 不 import `book_orchestrator`，不复用它的 `_looks_scanned` / `_readable_chars`，
  判据与阈值**独立写一份**。两边同时错才算漏判，比共用一份判据安全；
- 数据一律从 ORM 重新查（`book_agent_readings`），不用调用方传进来的 outcome 对象；
- 判据分级：`hard` 把这一轮算失败（计入熔断），`soft` 只告警（部分块失败、没读完）。
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.models.book import Book
from app.domain.models.book_agent_reading import BookAgentReading

# 有效正文的最小字数。低于它基本是「图片占位符 + 元数据」的扫描版/占位书。
# 刻意与 book_orchestrator._looks_scanned 的 1500 保持同量级但**各自定义**：
# 一处放宽不会连带把验证器一起放宽。
MIN_READABLE_CHARS = 1500
# 单书允许的要点提取失败比例：超过一半说明模型这一轮基本没干活
MAX_FAILED_RATIO = 0.5
# 总评低于这个长度多半是「未知」这类兜底文本，不算真读过
MIN_SUMMARY_CHARS = 40


@dataclass(frozen=True)
class Verdict:
    ok: bool
    level: str                       # pass | soft | hard
    reasons: tuple[str, ...] = ()

    def line(self) -> str:
        return "；".join(self.reasons) if self.reasons else "-"


def precheck(book: Book) -> str | None:
    """排队前的粗筛：这本书值不值得花 token。返回跳过原因，通过返回 None。

    这是原来**人手动做的那一步**——把《天才假象》这类扫描版挑出来别读。
    放在入队阶段做，而不是读完再判失败：省下的是一次注定被丢掉的模型调用，
    而且不会被计入「连续失败」把熔断器提前打开。
    """
    chars = int(book.total_chars or 0)
    if chars < MIN_READABLE_CHARS:
        return f"正文过短（{chars} 字，阈值 {MIN_READABLE_CHARS}），疑似占位或扫描版"
    if not (book.full_text or "").strip():
        return "全文为空"
    return None


def verify_reading(
    session: Session,
    book_id: str,
    *,
    user_id: str | None = None,
    min_chars: int = MIN_READABLE_CHARS,
    max_failed_ratio: float = MAX_FAILED_RATIO,
) -> Verdict:
    """从库里读回通读结果并逐项验。任何 hard 项成立即判失败。"""
    book = session.get(Book, book_id)
    if book is None:
        return Verdict(False, "hard", ("书不存在（可能已删除）",))

    stmt = select(BookAgentReading).where(BookAgentReading.book_id == book_id)
    if user_id:
        stmt = stmt.where(BookAgentReading.user_id == user_id)
    reading = session.scalars(stmt.order_by(BookAgentReading.updated_at.desc())).first()

    if reading is None or reading.is_deleted:
        # 扫描版 / 空文本时编排层不落记录，这里必须判失败——
        # 「没报错」不等于「读到了」，这正是要独立验证的理由。
        return Verdict(False, "hard", ("没有通读记录（编排层大概率判了 degraded）",))

    hard: list[str] = []
    soft: list[str] = []

    if int(book.total_chars or 0) < min_chars:
        hard.append(f"正文过短（{book.total_chars} 字，阈值 {min_chars}），读到的不是正文")
    if reading.status != "done":
        hard.append(f"通读记录状态为 {reading.status}")
    if int(reading.chunk_count or 0) <= 0:
        hard.append("chunk_count 为 0，一块都没读")

    indexes = [
        c.get("index") for c in (reading.chapters_note or [])
        if isinstance(c, dict) and isinstance(c.get("index"), int)
    ]
    if not indexes:
        hard.append("分章要点为空")
    else:
        dup = sorted({i for i in indexes if indexes.count(i) > 1})
        if dup:
            hard.append(f"块序号重复：{dup[:5]}")
        missing = sorted(set(range(int(reading.chunk_count or 0))) - set(indexes))
        if missing:
            # 有洞意味着断点合并丢了段：读出来的要点不覆盖全书，不能算读过
            hard.append(f"块序号空洞：缺 {missing[:5]}")
        extra = sorted(set(indexes) - set(range(int(reading.chunk_count or 0))))
        if extra:
            soft.append(f"存在超出 chunk_count 的块序号：{extra[:5]}")

    summary = (reading.summary or "").strip()
    if not summary:
        hard.append("总评为空")
    elif len(summary) < MIN_SUMMARY_CHARS:
        soft.append(f"总评过短（{len(summary)} 字），可能是兜底文本")

    failed = int(reading.failed_chunks or 0)
    chunks = max(1, int(reading.chunk_count or 0))
    if failed:
        if failed / chunks > max_failed_ratio:
            hard.append(f"要点提取失败 {failed}/{chunks} 块，超过 {max_failed_ratio:.0%}")
        else:
            soft.append(f"要点提取失败 {failed}/{chunks} 块，其余可用")
    if reading.truncated:
        soft.append("本次没读完（truncated），需续读")

    if hard:
        return Verdict(False, "hard", tuple(hard + soft))
    if soft:
        return Verdict(True, "soft", tuple(soft))
    return Verdict(True, "pass")
