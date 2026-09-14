"""FastAPI 入口：初始化日志/建库/挂请求中间件与路由。"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.endpoints import auth, books, chat, events, health, knowledge, l1, l2, l3, l4, l5, preferences, push
from app.core import logging as core_logging
from app.core import trace
from app.domain import db
from app.workers import runner

core_logging.setup_logging()
db.init_db()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """启动后台任务 worker（幂等/重试/死信；可用 worker_enabled 关闭）。"""
    runner.start_worker()
    yield


app = FastAPI(title="认知副驾 Cognitive Copilot", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """为每个请求注入 request_id，贯穿全链路（可靠/维护 需求）。"""
    rid = request.headers.get("X-Request-Id") or str(uuid.uuid4())
    with trace.request_id(rid):
        response = await call_next(request)
    response.headers["X-Request-Id"] = rid
    return response


# 参数校验失败的提示要能直接给人看：FastAPI 默认把 pydantic 的原始错误数组透出
# （`[{"type":"string_too_short","loc":["body","username"],"msg":"String should have
# at least 3 characters",...}]`），前端把它 JSON.stringify 后就是一整串英文 JSON，
# 直接怼在登录/注册的输入框下面。这里统一翻译成中文短句。
_FIELD_LABELS = {
    "username": "用户名",
    "password": "密码",
    "nickname": "昵称",
    "title": "标题",
    "content": "内容",
    "raw_content": "正文",
    "description": "描述",
    "priority": "优先级",
    "state": "状态",
    "accepted": "是否采纳",
    "push_frequency": "推送频率",
    "event_type": "事件类型",
    "item_id": "条目 ID",
    "conflict_id": "冲突 ID",
    "diagnosis_id": "诊断 ID",
    "goal_id": "目标 ID",
    "plan_id": "计划 ID",
    "job_id": "推送任务 ID",
    "page": "页码",
    "page_size": "每页条数",
    "limit": "条数上限",
}


def _validation_message(errors: list[dict]) -> str:
    """把 pydantic 校验错误数组翻译成中文短句（多条用「；」连接）。"""
    parts: list[str] = []
    for err in errors:
        loc = err.get("loc") or []
        # loc 形如 ("body", "username")，取最后一个业务字段名
        field = str(loc[-1]) if loc else "请求参数"
        label = _FIELD_LABELS.get(field, field)
        etype = str(err.get("type", ""))
        ctx = err.get("ctx") or {}
        if etype == "string_too_short":
            text = f"{label}至少 {ctx.get('min_length', 1)} 个字符"
        elif etype == "string_too_long":
            text = f"{label}最多 {ctx.get('max_length', 255)} 个字符"
        elif etype == "string_pattern_mismatch":
            text = f"{label}格式不正确（只允许字母、数字、_ . -）"
        elif etype == "missing":
            text = f"缺少{label}"
        elif etype.startswith("int_") or etype.startswith("float_"):
            text = f"{label}必须是数字"
        elif etype == "greater_than_equal":
            text = f"{label}不能小于 {ctx.get('ge', 0)}"
        elif etype == "less_than_equal":
            text = f"{label}不能大于 {ctx.get('le', 0)}"
        else:
            text = f"{label}填写不正确"
        if text not in parts:  # 同一字段的重复错误只提示一次
            parts.append(text)
    return "；".join(parts) or "请求参数不合法"


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "detail": _validation_message(exc.errors()),
            "request_id": trace.get_request_id() or "",
        },
    )


app.include_router(health.router)
app.include_router(auth.router)
app.include_router(chat.router)
app.include_router(knowledge.router)
app.include_router(books.router)
app.include_router(l1.router)
app.include_router(l2.router)
app.include_router(l3.router)
app.include_router(l4.router)
app.include_router(l5.router)
app.include_router(preferences.router)
app.include_router(push.router)
app.include_router(events.router)

WEB_DIR = Path(__file__).resolve().parents[1] / "web"

# 前端按功能拆成独立页面：登录 / 知识库 / L1 挖掘，共享 assets 下的样式与脚本。
# 同源托管（无需 CORS，也避免用 file:// 打开时的跨域限制）。
app.mount("/assets", StaticFiles(directory=WEB_DIR / "assets"), name="assets")


def _page(filename: str) -> FileResponse:
    return FileResponse(WEB_DIR / filename)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """入口为公开首页：可先浏览功能，点击使用再跳登录。"""
    return _page("index.html")


@app.get("/index.html", include_in_schema=False)
def index_page() -> FileResponse:
    return _page("index.html")


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


@app.get("/reading_log.html", include_in_schema=False)
def reading_log_page() -> FileResponse:
    return _page("reading_log.html")


@app.get("/conflicts.html", include_in_schema=False)
def conflicts_page() -> FileResponse:
    return _page("conflicts.html")


@app.get("/brief.html", include_in_schema=False)
def brief_page() -> FileResponse:
    return _page("brief.html")


@app.get("/l4.html", include_in_schema=False)
def l4_page() -> FileResponse:
    return _page("l4.html")


@app.get("/l5.html", include_in_schema=False)
def l5_page() -> FileResponse:
    return _page("l5.html")


@app.get("/notify.html", include_in_schema=False)
def notify_page() -> FileResponse:
    return _page("notify.html")