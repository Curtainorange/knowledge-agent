"""微信读书 → 本地知识库的同步。

产出形态刻意与「书籍摘录」**同构**：

- 每条划线、每条想法各成一条 KnowledgeItem；
- `raw_content` 沿用既有格式「原文 + 【我的想法】」——这样 L1 挖到的是**用户自己的
  理解**，而不只是划来的原文（与 `books/service.add_note` 同一约定）；
- `source_locator` 记下书、章节、range 与 deepLink，可跳回微信读书原文；
- `tags` 用「微信读书 / 划线 / 读书笔记 / 书评」区分内容类型。

幂等靠 `source_item_id`（划线用 bookmarkId、想法用 reviewId）：微信读书的 ID 稳定，
重复同步只会被跳过。**软删的条目也算「已存在」**——用户删掉的东西不该被下次同步复活。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.domain.models.knowledge_item import KnowledgeItem
from app.ingestion.service import IngestionService
from app.weread.client import WeReadClient, WeReadError

logger = logging.getLogger(__name__)

SOURCE = "weread"
_TAG_BASE = "微信读书"
_TITLE_MAX = 256
_THOUGHT_MARKER = "\n\n【我的想法】"


@dataclass
class SyncResult:
    """一次同步的结果统计（同时用于前端提示与事件埋点）。"""

    total_books: int = 0      # 微信读书里「有笔记」的书总数
    scanned_books: int = 0    # 本次真正扫描的书数
    created: int = 0          # 新入库条目数
    skipped: int = 0          # 已存在（含用户已删）而跳过的条目数
    failed_books: int = 0     # 单本拉取失败的书数（不中断整批）

    @property
    def pending_books(self) -> int:
        """因单次上限未扫描的书数 —— 再点一次同步即可继续。"""
        return max(0, self.total_books - self.scanned_books)

    def as_dict(self) -> dict:
        return {
            "total_books": self.total_books,
            "scanned_books": self.scanned_books,
            "created": self.created,
            "skipped": self.skipped,
            "failed_books": self.failed_books,
            "pending_books": self.pending_books,
        }


def build_client() -> WeReadClient:
    """按当前配置创建客户端；未配置 Key 时抛 WeReadNotConfigured。"""
    return WeReadClient(
        settings.weread_api_key,
        gateway_url=settings.weread_gateway_url,
        skill_version=settings.weread_skill_version,
        page_size=settings.weread_page_size,
        timeout=settings.weread_timeout_seconds,
        request_interval=settings.weread_request_interval_seconds,
    )


def _iso_from_unix(value) -> str:
    """微信读书的时间戳统一转成 ISO 字符串再进 locator（直接存数字没人看得懂）。"""
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(tzinfo=None).isoformat(" ")


def _join_title(book_title: str, part: str) -> str:
    text = f"{book_title} · {part}".strip(" ·") if part else book_title
    return (text or book_title or "微信读书")[:_TITLE_MAX]


def _find_first(node, key: str) -> str:
    """在嵌套结构里找第一个有值的 `key`。

    微信读书的想法回包层级不稳定（`reviewId` 挂在外层、正文在内层），
    写死层级迟早会漏字段，这里统一按「深度优先找第一个有值的」处理。
    """
    if isinstance(node, dict):
        value = node.get(key)
        if value not in (None, "", 0):
            return str(value).strip()
        for child in node.values():
            found = _find_first(child, key)
            if found:
                return found
    elif isinstance(node, list):
        for child in node:
            found = _find_first(child, key)
            if found:
                return found
    return ""


def _unwrap_review(wrapper) -> dict:
    """取出想法 / 点评的**内容对象**。

    官方文档写的是 `reviews[].review.content`（一层），但公开点评接口实测是
    `{idx, review: {reviewId, review: {content, abstract, ...}}}`（两层）：id 在外层、
    正文在内层。这里逐层下探到真正带 `content` 的那一层，两种写法都能吃。
    """
    node = wrapper
    for _ in range(3):
        if not isinstance(node, dict):
            return {}
        if "content" in node or "abstract" in node:
            return node
        inner = node.get("review")
        if not isinstance(inner, dict):
            return node
        node = inner
    return node if isinstance(node, dict) else {}


class WeReadSyncService:
    """把微信读书的划线 / 想法拉进本地知识库。

    `client` 与 `embedding` 可注入（测试用假件），默认按配置自建。
    """

    def __init__(self, session: Session, *, client=None, embedding=None):
        self._session = session
        self._client = client
        self._embedding = embedding

    # ---- 对外 ------------------------------------------------------------

    def sync(self, *, user_id: str, limit: int | None = None) -> SyncResult:
        client = self._client or build_client()
        cap = settings.weread_max_items_per_sync if limit is None else max(1, int(limit))

        result = SyncResult()
        notebooks = client.notebooks()
        result.total_books = len(notebooks)

        existing = self._existing_ids(user_id)
        collected: list[dict] = []
        seen: set[str] = set()

        for entry in notebooks:
            if len(collected) >= cap:
                break  # 到量即停：剩下的书留给下一次同步
            book_id = str(entry.get("bookId") or "").strip()
            if not book_id:
                continue
            result.scanned_books += 1
            try:
                candidates = self._collect_book(client, entry)
            except WeReadError as exc:
                result.failed_books += 1
                logger.warning("weread book skipped book=%s err=%s", book_id, exc)
                continue
            for cand in candidates:
                key = cand["source_item_id"]
                if key in existing or key in seen:
                    result.skipped += 1
                    continue
                seen.add(key)
                collected.append(cand)

        if len(collected) > cap:
            result.skipped += len(collected) - cap
            collected = collected[:cap]

        if collected:
            self._persist(user_id, collected)
            result.created = len(collected)

        self._record_event(user_id, result)
        return result

    def status(self, *, user_id: str) -> dict:
        """同步状态：是否已配置、已沉淀多少条、上次同步时间。"""
        from app.domain.repositories.knowledge_repository import KnowledgeRepository
        from app.domain.repositories.learning_event_repository import LearningEventRepository
        from app.feedback import events

        synced_items = KnowledgeRepository(self._session, user_id=user_id).count_by_source(
            user_id, SOURCE
        )
        last = LearningEventRepository(self._session, user_id=user_id).list_recent(
            user_id, event_type=events.WEREAD_SYNCED, limit=1
        )
        return {
            "configured": bool((settings.weread_api_key or "").strip()),
            "skill_version": settings.weread_skill_version,
            "synced_items": synced_items,
            "last_synced_at": last[0].occurred_at if last else None,
        }

    # ---- 内部 ------------------------------------------------------------

    def _existing_ids(self, user_id: str) -> set[str]:
        """本项目里已存在的微信读书 item id。

        刻意**不过滤 is_deleted**：用户主动删掉的条目应当保持删除状态，
        不能被下一次同步悄悄复活。
        """
        stmt = select(KnowledgeItem.source_item_id).where(
            KnowledgeItem.user_id == user_id,
            KnowledgeItem.source == SOURCE,
            KnowledgeItem.source_item_id.is_not(None),
        )
        return {str(value) for value in self._session.scalars(stmt) if value}

    def _collect_book(self, client, entry: dict) -> list[dict]:
        """把一本书的划线 + 想法翻成候选知识条目。"""
        book_id = str(entry.get("bookId") or "").strip()
        meta = entry.get("book") or {}
        book_title = (meta.get("title") or "").strip()
        author = (meta.get("author") or "").strip()
        deep_link = (meta.get("deepLink") or "").strip()

        # 书籍详情用于补全书名 / 作者 / deepLink；失败不影响划线同步
        try:
            info = client.book_info(book_id) or {}
        except WeReadError as exc:
            logger.warning("weread book_info failed book=%s err=%s", book_id, exc)
            info = {}
        book_title = (info.get("title") or book_title).strip() or f"微信读书 {book_id}"
        author = (info.get("author") or author).strip()
        deep_link = (info.get("deepLink") or deep_link).strip()

        def locator(**extra) -> dict:
            data = {
                "provider": "weread",
                "book_id": book_id,
                "book_title": book_title,
                "author": author,
                "deep_link": deep_link,
            }
            data.update(extra)
            return data

        items: list[dict] = []

        marks = client.bookmarks(book_id) or {}
        chapters = {c.get("chapterUid"): c for c in (marks.get("chapters") or [])}
        for mark in marks.get("updated") or []:
            text = (mark.get("markText") or "").strip()
            bookmark_id = str(mark.get("bookmarkId") or "").strip()
            if not text or not bookmark_id:
                continue
            chapter = chapters.get(mark.get("chapterUid")) or {}
            chapter_title = (chapter.get("title") or "").strip()
            items.append({
                "source_item_id": f"hl:{bookmark_id}",
                "title": _join_title(book_title, chapter_title),
                "content": text,
                "note": "",
                "tags": [_TAG_BASE, "划线"],
                "locator": locator(
                    kind="highlight",
                    chapter_uid=mark.get("chapterUid"),
                    chapter_index=chapter.get("chapterIdx"),
                    chapter_title=chapter_title,
                    range=mark.get("range"),
                    marked_at=_iso_from_unix(mark.get("createTime")),
                ),
            })

        for wrapper in client.my_reviews(book_id) or []:
            review = _unwrap_review(wrapper)
            content = (review.get("content") or "").strip()
            review_id = _find_first(wrapper, "reviewId")
            if not content or not review_id:
                continue
            abstract = (review.get("abstract") or "").strip()
            chapter_name = (review.get("chapterName") or "").strip()
            if abstract:
                # 划线想法：与书籍摘录同一格式，让 L1 能挖到「你的理解」
                raw = f"{abstract}{_THOUGHT_MARKER}{content}"
                tags = [_TAG_BASE, "读书笔记"]
                part = chapter_name or "想法"
            else:
                # 整本书评 / 章节点评：没有对应原文，本身就是一条独立想法
                raw = content
                tags = [_TAG_BASE, "书评"]
                part = chapter_name or "书评"
            items.append({
                "source_item_id": f"rv:{review_id}",
                "title": _join_title(book_title, part),
                "content": raw,
                "note": content if abstract else "",
                "tags": tags,
                "locator": locator(
                    kind="review",
                    chapter_uid=review.get("chapterUid"),
                    chapter_index=review.get("chapterIdx"),
                    chapter_title=chapter_name,
                    range=review.get("range"),
                    marked_at=_iso_from_unix(review.get("createTime")),
                ),
            })
        return items

    def _persist(self, user_id: str, candidates: list[dict]) -> None:
        """逐条走 IngestionService —— 保持「唯一摄入入口」的一致性。

        代价是每条都要过一次向量计算与 L2 触发检查；换来的是每条知识都有
        KNOWLEDGE_CREATED 埋点、embed_status 语义与手动录入完全一致。
        """
        svc = IngestionService(self._session, self._embedding)
        for cand in candidates:
            item = svc.add_knowledge(
                user_id=user_id,
                title=cand["title"],
                content=cand["content"],
                source=SOURCE,
                tags=cand["tags"],
            )
            # add_knowledge 只认 title/content/source/tags，其余字段在这里补齐
            item.source_item_id = cand["source_item_id"]
            item.note = cand["note"]
            item.source_locator = cand["locator"]
        self._session.commit()

    def _record_event(self, user_id: str, result: SyncResult) -> None:
        from app.feedback import events

        events.record(
            self._session,
            user_id=user_id,
            event_type=events.WEREAD_SYNCED,
            payload=result.as_dict(),
        )
