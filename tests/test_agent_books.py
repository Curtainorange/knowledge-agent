"""书籍三项接进对话的测试（全 Mock，不触网）：通读 / 讨论 / 推荐。

这份测试重点钉四件事：

1. **隔离不变量**：智能体通读全书后，用户的阅读进度（books.read_progress /
   current_char）与知识库（knowledge_items）必须**分毫未动**——「智能体读的书」
   和「用户读的书」在数据层是两本账，这是这条能力线的第一设计约束。
2. **通读有成本护栏**：书没变（total_chars 相同）时复用既有笔记，不再花一次模型钱。
3. **讨论必须有依据**：没通读过的书不给讨论（宁可提示先通读），回复必须基于通读笔记。
4. **推荐不联网**：画像为空就明说「没有依据」，不硬编一份通用书单。
"""
from __future__ import annotations

import json
from urllib.parse import quote

import pytest
from sqlalchemy import select

from app.agent import turns
from app.agent.router import _match_local
from app.api import deps
from app.core.config import settings
from app.domain.models.book import Book
from app.domain.models.book_agent_reading import BookAgentReading
from app.domain.models.knowledge_item import KnowledgeItem
from app.domain.models.task_run import TaskRun
from app.domain.repositories.book_agent_reading_repository import BookAgentReadingRepository
from app.llm.completion import Completion
from app.llm.gateway import ModelGateway
from app.llm.provider import LLMProvider
from app.main import app
from tests.helpers import sign_in

SAMPLE_TXT = "第一章 起点\n这是第一章的内容。\n\n第二章 转折\n这是第二章的内容。"


class ScriptedProvider(LLMProvider):
    def __init__(self) -> None:
        self.rows: list[str] = []
        self.task_types: list[str] = []
        # 记下每次调用的 messages：讨论是否真的把原文片段送进 prompt，只能从这里看
        self.calls: list[list[dict]] = []

    def chat(
        self, *, model, reasoning, messages, tools=None, response_format=None, task_type="default"
    ) -> Completion:
        self.task_types.append(task_type)
        self.calls.append(messages)
        text = self.rows.pop(0) if self.rows else "（脚本已用尽）"
        return Completion(
            text=text, prompt_tokens=1, completion_tokens=1, finish_reason="stop",
            model=model, reasoning=reasoning,
        )

    def last_user_message(self) -> str:
        """最近一次调用里的 user 内容——断言 prompt 拼装用。"""
        if not self.calls:
            return ""
        for message in reversed(self.calls[-1]):
            if message.get("role") == "user":
                return message.get("content") or ""
        return ""


@pytest.fixture()
def provider():
    scripted = ScriptedProvider()

    def override_gateway():
        return ModelGateway(provider=scripted)

    app.dependency_overrides[deps.get_gateway] = override_gateway
    yield scripted
    app.dependency_overrides.pop(deps.get_gateway, None)


def _upload(client, headers, filename: str, content: bytes) -> str:
    resp = client.post(
        "/api/v1/books",
        content=content,
        headers={
            **headers,
            "X-Filename": quote(filename),
            "Content-Type": "application/octet-stream",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["book_id"]


def _send(client, headers, message, conversation_id=None):
    resp = client.post(
        "/api/v1/agent/chat",
        json={"conversation_id": conversation_id, "message": message},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _seed_reading(session, user_id: str, book_id: str, *, total_chars: int) -> BookAgentReading:
    reading = BookAgentReadingRepository(session, user_id=user_id).upsert(
        book_id=book_id,
        status="done",
        total_chars=total_chars,
        summary="全书总评：围绕方法与修正展开。",
        chapters_note=[{"index": 0, "title": "第一章 起点", "gist": "交代背景", "points": ["要点甲"]}],
        chunk_count=1,
        failed_chunks=0,
        truncated=False,
    )
    session.commit()
    return reading


# ---- 本地规则 ---------------------------------------------------------------


def test_local_rules_hit_book_capabilities():
    route = _match_local("通读《三体》")
    assert route is not None and route.capability == "book_digest"
    assert route.args["title"] == "三体"
    assert route.args["force"] is False

    route = _match_local("重新通读《三体》")
    assert route is not None and route.capability == "book_digest"
    assert route.args["force"] is True

    route = _match_local("聊聊这本书")
    assert route is not None and route.capability == "book_discuss"

    route = _match_local("再推荐几本书")
    assert route is not None and route.capability == "book_recommend"

    # 词形相近但不带对应意图的说法不该被硬塞：查看书架仍是 books
    assert _match_local("我的书架里有什么").capability == "books"


# ---- 通读 --------------------------------------------------------------------


def test_digest_end_to_end_isolated_from_user_reading(client, session, provider):
    """通读完成：笔记落独立表，用户阅读进度与知识库分毫未动。"""
    user = sign_in(client, "book_digest_e2e")
    book_id = _upload(client, user.headers, "测试书.txt", SAMPLE_TXT.encode("utf-8"))

    # 两章合计远小于单块上限 → 贪心合并成 1 块：1 次提要点 + 1 次汇总
    provider.rows = [
        json.dumps({"gist": "第一章交代背景，第二章推进转折", "points": ["要点甲", "要点乙", "要点丙"]}),
        json.dumps({"summary": "全书围绕起点与转折展开，主张先立框架再谈细节。"}),
    ]

    body = _send(client, user.headers, "通读《测试书》")
    assert body["capability"] == "book_digest"
    card = body["card"]
    assert card["kind"] == "book_digest"
    assert card["state"] == "ok"
    assert card["summary"].startswith("全书围绕")
    assert len(card["chapters"]) == 1
    assert card["sends"] and "聊聊" in card["sends"][0]["message"]
    assert any(s["message"] == "扫描知识冲突" for s in card["sends"]), (
        "通读卡要有「顺手扫一遍冲突」按钮，把新书观点立刻和笔记对撞"
    )

    # 隔离不变量：智能体的阅读成果只落在独立表里
    reading = BookAgentReadingRepository(session, user_id=user.user_id).get_by_book(book_id)
    assert reading is not None and reading.status == "done"
    assert reading.chunk_count == 1 and reading.failed_chunks == 0

    items = list(session.scalars(
        select(KnowledgeItem).where(KnowledgeItem.user_id == user.user_id)
    ))
    assert items == [], "通读不允许往知识库写任何东西"

    book = session.get(Book, book_id)
    assert float(book.read_progress) == 0.0 and book.current_char == 0, (
        "通读不允许动用户的阅读进度"
    )


def test_digest_reuses_notes_when_book_unchanged(client, session, provider):
    """书没变时复用既有笔记：不花一次模型调用，卡片标 reused。"""
    user = sign_in(client, "book_digest_reuse")
    book_id = _upload(client, user.headers, "测试书.txt", SAMPLE_TXT.encode("utf-8"))
    _seed_reading(session, user.user_id, book_id, total_chars=len(SAMPLE_TXT))

    body = _send(client, user.headers, "通读《测试书》")
    assert body["card"]["kind"] == "book_digest"
    assert body["card"]["state"] == "reused"
    assert provider.task_types == [], "复用路径不允许再调模型"
    assert "已经通读过" in body["reply"]


def test_digest_without_title_offers_shelf(client, provider):
    user = sign_in(client, "book_digest_pick")
    _upload(client, user.headers, "测试书.txt", SAMPLE_TXT.encode("utf-8"))

    body = _send(client, user.headers, "通读一本书")
    card = body["card"]
    assert card["kind"] == "notice"
    assert card["sends"] and card["sends"][0]["message"] == "通读《测试书》"
    assert provider.task_types == [], "选书不需要模型"


def test_digest_with_unknown_title_offers_shelf(client, provider):
    user = sign_in(client, "book_digest_unknown")
    _upload(client, user.headers, "测试书.txt", SAMPLE_TXT.encode("utf-8"))

    body = _send(client, user.headers, "通读《不存在的书》")
    card = body["card"]
    assert card["kind"] == "notice"
    assert any(s["message"] == "通读《测试书》" for s in card["sends"])


def test_digest_rejects_scanned_book(session):
    """扫描图片版没有正文：判 degraded，且一次模型都不调。

    回归：原来只在「切不出块」时降级；扫描版能切出 1 块元数据，于是「成功」
    产出一份封面占位符摘要，看着像读过，实际一个字都没读到。
    """
    from app.agent import book_orchestrator

    provider = ScriptedProvider()
    text = " ".join(f"[[IMG:img_{i:04d}.jpg]]" for i in range(120)) + " Anna's Archive 共 209 页"
    book = Book(
        id="scan1", user_id="u1", title="扫描书", author="", format="epub",
        file_path="x.epub", chapters=[], full_text=text, total_chars=len(text),
    )
    session.add(book)
    session.commit()

    out = book_orchestrator.read_whole_book(
        session, ModelGateway(provider=provider), user_id="u1", book=book
    )
    assert out.state == "degraded"
    assert "正文" in out.note
    assert provider.task_types == [], "扫描版不该调模型"


def test_digest_resumes_from_breakpoint(session):
    """长书分两次读：第二次从断点接着读，合并后要点覆盖全书。"""
    from app.agent import book_orchestrator

    text = "正" * 18_000  # 切成 2 块
    book = Book(
        id="resume1", user_id="u1", title="长书", author="", format="txt",
        file_path="x.txt", chapters=[], full_text=text, total_chars=len(text),
    )
    session.add(book)
    session.commit()

    chunk_row = json.dumps({"topic": "测试", "gist": "这一块的要点", "points": ["p"]}, ensure_ascii=False)
    summary_row = json.dumps({"summary": "全书总评"}, ensure_ascii=False)
    provider = ScriptedProvider()
    provider.rows = [chunk_row, summary_row, chunk_row, summary_row]

    first = book_orchestrator.read_whole_book(
        session, ModelGateway(provider=provider), user_id="u1", book=book, max_chunks=1
    )
    assert first.truncated is True
    assert first.chunk_count == 1
    assert len(first.chapters) == 1
    assert "还剩 1 块" in first.note

    second = book_orchestrator.read_whole_book(
        session, ModelGateway(provider=provider), user_id="u1", book=book
    )
    assert second.truncated is False
    assert second.chunk_count == 2
    assert [c["index"] for c in second.chapters] == [0, 1], "续读要合并上次的要点"
    assert second.summary == "全书总评"


# ---- 讨论 --------------------------------------------------------------------


def test_discuss_after_digest_answers_from_notes(client, session, provider):
    user = sign_in(client, "book_discuss_ok")
    book_id = _upload(client, user.headers, "测试书.txt", SAMPLE_TXT.encode("utf-8"))
    _seed_reading(session, user.user_id, book_id, total_chars=len(SAMPLE_TXT))

    provider.rows = ["这本书的主线是先立框架再谈细节，第二章是转折点。"]
    body = _send(client, user.headers, "聊聊《测试书》这本书的主线")
    assert body["capability"] == "book_discuss"
    assert body["card"] is None, "讨论是自然语言回复，不带卡片"
    assert "主线" in body["reply"]
    assert provider.task_types == ["multi_turn_dialogue"]


def test_discuss_without_digest_prompts_to_read_first(client, provider):
    user = sign_in(client, "book_discuss_none")
    _upload(client, user.headers, "测试书.txt", SAMPLE_TXT.encode("utf-8"))

    body = _send(client, user.headers, "聊聊《测试书》这本书")
    card = body["card"]
    assert card["kind"] == "notice"
    assert card["sends"] and card["sends"][0]["message"] == "通读《测试书》"
    assert provider.task_types == []


# ---- 原文检索（A1）：讨论要能查到原句，而不是只有摘要 --------------------------

DISCUSS_TXT = (
    "第一章 起点\n"
    "刻意练习的核心是有目标的练习，并在练习中拿到即时反馈。\n\n"
    "第二章 心理表征\n"
    "心理表征是大脑对事物的内部编码方式，高手靠它预判局面。\n\n"
    "第三章 一万小时\n"
    "一万小时定律只是粗略的概括，练习的质量比时长更关键。"
)


def test_discuss_sends_retrieved_excerpts(client, session, provider):
    """讨论必须把检索到的原文片段一起送进 prompt——否则细节问题只能靠摘要编。"""
    user = sign_in(client, "book_discuss_excerpt")
    book_id = _upload(client, user.headers, "片段书.txt", DISCUSS_TXT.encode("utf-8"))
    _seed_reading(session, user.user_id, book_id, total_chars=len(DISCUSS_TXT))

    provider.rows = ["书里说得很明确：质量比时长更关键。"]
    body = _send(client, user.headers, "聊聊《片段书》里讲的一万小时定律")

    assert body["capability"] == "book_discuss"
    prompt = provider.last_user_message()
    assert "【原文片段】" in prompt, "检索结果必须进 prompt"
    assert "一万小时定律只是粗略的概括" in prompt, "命中的原句要原样带过去"
    assert "【通读笔记】" in prompt, "摘要与原文两路并用，不是二选一"


def test_discuss_rebuilds_missing_chunk_index(client, session, provider):
    """块索引缺失（老书）时讨论不能挂：懒回填后照常检索。"""
    from sqlalchemy import delete

    from app.domain.models.book_chunk import BookChunk

    user = sign_in(client, "book_discuss_rebuild")
    book_id = _upload(client, user.headers, "老书.txt", DISCUSS_TXT.encode("utf-8"))
    _seed_reading(session, user.user_id, book_id, total_chars=len(DISCUSS_TXT))

    # 模拟 0023 迁移之前入库的书：表里没有任何块
    session.execute(delete(BookChunk).where(BookChunk.book_id == book_id))
    session.commit()

    provider.rows = ["按书里的意思，心理表征是内部编码。"]
    body = _send(client, user.headers, "聊聊《老书》里说的心理表征是什么")

    assert body["capability"] == "book_discuss"
    assert "【原文片段】" in provider.last_user_message()
    # 懒回填真的落库了，不是每次现切
    assert session.scalar(
        select(BookChunk.id).where(BookChunk.book_id == book_id).limit(1)
    ) is not None


def test_upload_builds_chunk_index(client, session):
    """上传时就把分块索引建好：讨论时才不用现切，块编号也稳定。"""
    from app.domain.models.book_chunk import BookChunk

    user = sign_in(client, "book_chunks_upload")
    book_id = _upload(client, user.headers, "索引书.txt", DISCUSS_TXT.encode("utf-8"))

    chunks = list(session.scalars(select(BookChunk).where(BookChunk.book_id == book_id)))
    assert chunks, "上传后应有分块"
    assert [c.seq for c in chunks] == sorted(c.seq for c in chunks)
    # 块的字符区间必须能在原文上切出内容（引用、页码换算都依赖它）
    book = session.get(Book, book_id)
    for chunk in chunks:
        assert book.full_text[chunk.char_start:chunk.char_end] == chunk.text


# ---- 推荐 --------------------------------------------------------------------


def test_recommend_builds_card_from_profile(client, session, provider):
    from app.domain.repositories.knowledge_repository import KnowledgeRepository

    user = sign_in(client, "book_recommend_ok")
    KnowledgeRepository(session, user_id=user.user_id).create(
        user_id=user.user_id, title="索引笔记", content="B+树适合范围查询 #数据库"
    )
    session.commit()

    provider.rows = [
        json.dumps({
            "overview": "你的笔记集中在数据库上，先补一本系统底层的书。",
            "items": [
                {"title": "数据密集型应用系统设计", "author": "Martin Kleppmann",
                 "fit": "回应你索引笔记里的主张", "reason": "前两章把存储引擎取舍讲透。"},
                {"title": "深度工作", "author": "Cal Newport",
                 "fit": "配合你的学习目标", "reason": "讲如何安排不受打扰的深度学习时段。"},
            ],
        })
    ]

    body = _send(client, user.headers, "推荐几本书")
    assert body["capability"] == "book_recommend"
    card = body["card"]
    assert card["kind"] == "book_recommend"
    assert card["state"] == "ok"
    assert len(card["items"]) == 2
    assert card["items"][0]["fit"], "fit 必须挂在学习画像上"
    assert card["sends"] and card["sends"][0]["label"] == "换一批"
    assert "挑了 2 本" in body["reply"]


def test_profile_includes_agent_summary(session):
    """推荐画像里的已通读书，要带一句话摘要，而不是只有书名。

    回归：推荐靠模型自由发挥，只喂书名模型只能凭记忆硬编承接关系；
    带上摘要后，模型才能「接着他读过的那本的内容」说。
    """
    from app.agent.book_orchestrator import _profile

    book = Book(
        user_id="u1", title="刻意练习", author="安德斯",
        format="txt", file_path="x.txt",
        chapters=[], full_text="x", total_chars=1,
    )
    session.add(book)
    session.commit()
    session.add(BookAgentReading(
        user_id="u1", book_id=book.id, status="done",
        total_chars=1, summary="走出舒适区进行有目的练习。",
        chapters_note=[], chunk_count=1, failed_chunks=0, truncated=False,
    ))
    session.commit()

    read = _profile(session, user_id="u1")["agent_read_books"]
    assert read, "已通读的书应进画像"
    assert read[0]["title"] == "刻意练习"
    assert "舒适区" in read[0]["summary"]


def test_recommend_empty_profile_says_so(client, provider):
    user = sign_in(client, "book_recommend_empty")

    body = _send(client, user.headers, "推荐几本书")
    card = body["card"]
    assert card["kind"] == "book_recommend"
    assert card["state"] == "empty"
    assert card["items"] == []
    assert "空" in card["note"]
    assert provider.task_types == [], "画像为空时不该调模型"


# ---- 切块 --------------------------------------------------------------------


def test_build_chunks_merges_fragmented_chapters():
    """epub 碎片章节（目录、版权页各算一章）必须按**字数**合并成块。

    否则块上限会被章节数顶满——真实案例：《世界尽头的咖啡馆》3.6 万字被切成
    16+ 块、后 7 章直接截断没读。
    """
    from app.agent.book_orchestrator import _build_chunks

    chapters = [
        {"index": i, "title": f"第{i}节", "char_start": i * 3000, "char_end": (i + 1) * 3000}
        for i in range(30)
    ]
    book = Book(
        id="b1", user_id="u1", title="t", author="", format="txt", file_path="",
        chapters=chapters, full_text="x" * 90000, total_chars=90000,
    )
    chunks, truncated = _build_chunks(book)
    assert not truncated, "9 万字的书不该被截断"
    assert len(chunks) == 8, "9 万字应合并成 8 块（7 块满 + 1 块半）"
    assert "等" in chunks[0]["title"], "合并块的标题要说明它含多节"
    assert all(len(c["text"]) <= 12000 for c in chunks), "单块不得超过上限"


def test_build_chunks_returns_all_chunks_and_flags_over_limit():
    """切块不再截断：续读要知道全书一共多少块、上次读到哪。"""
    from app.agent.book_orchestrator import MAX_CHUNKS, _build_chunks

    book = Book(
        id="b2", user_id="u1", title="t", author="", format="txt", file_path="",
        chapters=[], full_text="x" * (12000 * (MAX_CHUNKS + 5)), total_chars=0,
    )
    chunks, over_limit = _build_chunks(book)
    assert over_limit is True
    assert len(chunks) == MAX_CHUNKS + 5, "全部块都要返回，截断交给 read_whole_book"


def test_readable_chars_strips_image_placeholders():
    """扫描版的「全文」是图片占位符，原始字数看着有几千，有效字数为零。"""
    from app.agent.book_orchestrator import _readable_chars

    assert _readable_chars("[[IMG:img_0001.jpg]]" * 50) == 0
    assert _readable_chars("正常正文" * 100) == 400


# ---- 异步入队 ----------------------------------------------------------------


def test_book_heavy_capabilities_are_queued_when_worker_enabled(client, session, monkeypatch):
    """worker 开着时，通读/推荐只发 pending 卡并排队（与 l2/l3 同一批）。"""
    monkeypatch.setattr(settings, "worker_enabled", True)
    user = sign_in(client, "book_queue")

    body = _send(client, user.headers, "推荐几本书")
    assert body["capability"] == "book_recommend"
    assert body["card"]["kind"] == "pending"

    session.flush()
    queued = list(session.scalars(
        select(TaskRun).where(TaskRun.task_name == turns.TASK_NAME)
    ))
    assert queued, "没有入队，后台永远不会跑"
    assert queued[-1].payload["capability"] == "book_recommend"
