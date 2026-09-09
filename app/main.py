"""FastAPI 入口：初始化日志/建库/挂请求中间件与路由。"""
from __future__ import annotations

import uuid

from fastapi import FastAPI, Request

from app.api.endpoints import chat, health, knowledge, l1
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
app.include_router(chat.router)
app.include_router(knowledge.router)
app.include_router(l1.router)