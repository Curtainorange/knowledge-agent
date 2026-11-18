"""L1 认知挖掘端点（D3）：发起/承接多轮澄清挖掘。

两个入口，同一件事的两个检索面：
- `/mine`：在**用户自己的知识库**里按线索定位条目；
- `/book-search`：库里没有时，**去网上**把「只记得情节/作者、不记得书名」的书找出来。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.agent import book_finder
from app.agent.l1_orchestrator import L1Orchestrator
from app.api.deps import get_gateway, get_session, get_user_id
from app.core import trace
from app.llm.gateway import ModelGateway

router = APIRouter(prefix="/api/v1/l1", tags=["l1"])


class L1MineRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = None


class LocatedItemOut(BaseModel):
    """判定命中的条目：带摘要与阅读进度，前端无需再发一次详情请求。"""

    item_id: str
    title: str
    read_progress: float
    snippet: str = ""
    embed_status: str = ""


class CandidateOut(BaseModel):
    """召回候选明细。

    **包含**已命中项——它就是本次召回的完整视图，调用方展示「其他可能」时
    自行排除 `located_items` 中的 id。保留完整列表是为了让调用方能看到
    命中项之外还有什么，以及各自的检索得分与命中通道。
    """

    item_id: str
    title: str
    snippet: str
    score: float
    channels: list[str] = Field(default_factory=list)
    read_progress: float = 0.0


class L1MineResponse(BaseModel):
    state: str  # located | clarifying | empty
    conversation_id: str
    question: str = ""
    located_items: list[LocatedItemOut] = Field(default_factory=list)
    candidates: list[CandidateOut] = Field(default_factory=list)
    read_hint: str = ""
    turn: int = 0
    max_turns: int = 3
    reason: str = ""
    request_id: str


@router.post("/mine", response_model=L1MineResponse)
def l1_mine(
    body: L1MineRequest,
    user_id: str = Depends(get_user_id),
    gateway: ModelGateway = Depends(get_gateway),
    session: Session = Depends(get_session),
) -> L1MineResponse:
    orchestrator = L1Orchestrator(gateway, session)
    result = orchestrator.mine(user_id=user_id, conversation_id=body.conversation_id, message=body.message)
    return L1MineResponse(
        state=result.state,
        conversation_id=result.conversation_id,
        question=result.question,
        located_items=[
            LocatedItemOut(
                item_id=item.item_id,
                title=item.title,
                read_progress=item.read_progress,
                snippet=item.snippet,
                embed_status=item.embed_status,
            )
            for item in result.located_items
        ],
        candidates=[
            CandidateOut(
                item_id=candidate.item_id,
                title=candidate.title,
                snippet=candidate.snippet,
                score=candidate.score,
                channels=candidate.channels,
                read_progress=candidate.read_progress,
            )
            for candidate in result.candidates
        ],
        read_hint=result.read_hint,
        turn=result.turn,
        max_turns=result.max_turns,
        reason=result.reason,
        request_id=trace.get_request_id() or "",
    )


# ---- 全网找书 ---------------------------------------------------------------
#
# 无状态：不写会话、不写知识库。前端把「之前说过什么」「已经看过哪些书」带上来，
# 候选只活在这一次响应里。用户要留下哪本，自己点「记入知识库」走既有录入链路。


class BookSearchRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000, description="这次补充的线索")
    history: list[str] = Field(
        default_factory=list, max_length=10, description="前几轮说过的线索（越靠后越新）"
    )
    exclude: list[str] = Field(
        default_factory=list, max_length=20, description="已经看过、用户说不是的书名"
    )


class BookSearchBookOut(BaseModel):
    title: str
    author: str = ""
    year: str = ""
    why: str = ""
    confidence: float = 0.0
    url: str = ""
    site: str = ""
    # 去哪拿：来自本地离线书目（`app/books/catalog.py`），查不到时为空。
    # `has_epub` 是给前端判断「能不能直接进阅读器」的——上传白名单只认 epub/txt/pdf。
    resource_url: str = ""
    resource_category: str = ""
    has_epub: bool = False


class BookSearchResponse(BaseModel):
    state: str  # found | clarify | not_found | search_failed | unavailable | failed
    books: list[BookSearchBookOut] = Field(default_factory=list)
    summary: str = ""
    question: str = ""
    note: str = ""
    queries: list[str] = Field(default_factory=list)
    searched: int = 0
    request_id: str


@router.post("/book-search", response_model=BookSearchResponse)
def l1_book_search(
    body: BookSearchRequest,
    user_id: str = Depends(get_user_id),
    gateway: ModelGateway = Depends(get_gateway),
    session: Session = Depends(get_session),
) -> BookSearchResponse:
    result = book_finder.find_books(
        gateway=gateway,
        session=session,
        user_id=user_id,
        message=body.message,
        history=body.history,
        exclude=body.exclude,
    )
    return BookSearchResponse(
        state=result.state,
        books=[
            BookSearchBookOut(
                title=book.title,
                author=book.author,
                year=book.year,
                why=book.why,
                confidence=book.confidence,
                url=book.url,
                site=book.site,
                resource_url=book.resource_url,
                resource_category=book.resource_category,
                has_epub=book.has_epub,
            )
            for book in result.books
        ],
        summary=result.summary,
        question=result.question,
        note=result.note,
        queries=result.queries,
        searched=result.searched,
        request_id=trace.get_request_id() or "",
    )