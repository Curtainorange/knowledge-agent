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

    def chat(
        self, *, model, reasoning, messages, tools=None, response_format=None, task_type="default"
    ) -> Completion:
        self.task_types.append(task_type)
        text = self.rows.pop(0) if self.rows else "（脚本已用尽）"
        return Completion(
            text=text, prompt_tokens=1, completion_tokens=1, finish_reason="stop",
            model=model, reasoning=reasoning,
        )


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


def test_build_chunks_truncates_oversized_book():
    from app.agent.book_orchestrator import MAX_CHUNKS, _build_chunks

    book = Book(
        id="b2", user_id="u1", title="t", author="", format="txt", file_path="",
        chapters=[], full_text="x" * (12000 * (MAX_CHUNKS + 5)), total_chars=0,
    )
    chunks, truncated = _build_chunks(book)
    assert truncated and len(chunks) == MAX_CHUNKS


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
