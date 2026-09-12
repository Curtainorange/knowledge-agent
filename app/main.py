"""FastAPI 入口：初始化日志/建库/挂请求中间件与路由。"""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.endpoints import auth, books, chat, health, knowledge, l1
from app.core import logging as core_logging
from app.core import trace
from app.domain import db

core_logging.setup_logging()
db.init_db()

app = FastAPI(title="认知副驾 Cognitive Copilot", version="0.1.0")


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """为每个请求注入 request_id，贯穿全链路（可靠/维护 需求）。"""
    rid = request.headers.get("X-Request-Id") or str(uuid.uuid4())
    with trace.request_id(rid):
        response = await call_next(request)
    response.headers["X-Request-Id"] = rid
    return response


app.include_router(health.router)
app.include_router(auth.router)
app.include_router(chat.router)
app.include_router(knowledge.router)
app.include_router(books.router)
app.include_router(l1.router)

WEB_DIR = Path(__file__).resolve().parents[1] / "web"

# 前端按功能拆成独立页面：登录 / 知识库 / L1 挖掘，共享 assets 下的样式与脚本。
# 同源托管（无需 CORS，也避免用 file:// 打开时的跨域限制）。
app.mount("/assets", StaticFiles(directory=WEB_DIR / "assets"), name="assets")


def _page(filename: str) -> FileResponse:
    return FileResponse(WEB_DIR / filename)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """入口给登录页；已有有效令牌时由页面脚本自行跳到知识库。"""
    return _page("login.html")


@app.get("/login.html", include_in_schema=False)
def login_page() -> FileResponse:
    return _page("login.html")


@app.get("/knowledge.html", include_in_schema=False)
def knowledge_page() -> FileResponse:
    return _page("knowledge.html")


@app.get("/mine.html", include_in_schema=False)
def mine_page() -> FileResponse:
    return _page("mine.html")


@app.get("/books.html", include_in_schema=False)
def books_page() -> FileResponse:
    return _page("books.html")


@app.get("/reader.html", include_in_schema=False)
def reader_page() -> FileResponse:
    return _page("reader.html")