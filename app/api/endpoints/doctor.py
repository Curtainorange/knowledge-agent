"""系统自检端点（doctor，借鉴 agentic-local-brain 的 `localbrain doctor`）。

一次请求回答「这个系统现在能不能正常干活」：数据库、模型网关、嵌入模型、
向量索引四项逐个体检。给排查「功能全崩」类问题一个统一入口，不用再凭感觉
猜是 Key 丢了、模型挂了还是索引空了。

需要登录：自检结果暴露供应商配置等内部信息，不给匿名访客看。
单项检查失败不拖垮整体——每项独立 try，失败记录原因，状态置 degraded。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import get_session, get_user_id
from app.core.config import settings
from app.llm.gateway import ModelGateway
from app.retrieval.embedding import build_embedding
from app.retrieval.vector_store import build_vector_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/system", tags=["system"])


def _check_database(session: Session) -> dict:
    try:
        session.execute(text("SELECT 1"))
        return {"ok": True, "detail": "数据库连通"}
    except Exception as exc:  # noqa: BLE001 - 自检不允许抛
        return {"ok": False, "detail": f"数据库异常：{exc}"}


def _check_llm() -> dict:
    gateway = ModelGateway()
    if gateway.has_real_provider:
        return {"ok": True, "detail": f"真实供应商：{'、'.join(gateway.real_provider_names)}"}
    return {
        "ok": False,
        "detail": "未配置模型 Key（MIMO_API_KEY / DEEPSEEK_API_KEY），LLM 能力走规则降级档",
    }


def _check_embedding() -> dict:
    backend = settings.embedding_backend
    try:
        embedding = build_embedding()
        vec = embedding.embed(["连通性探针"])[0]
        return {
            "ok": True,
            "detail": f"后端 {backend}，模型 {embedding.name}，dim={len(vec)}",
        }
    except Exception as exc:  # noqa: BLE001 - 模型加载失败也只降级，不拖垮自检
        return {"ok": False, "detail": f"嵌入模型不可用（{backend}）：{exc}"}


def _check_vector_store(session: Session) -> dict:
    backend = settings.vector_backend
    try:
        store = build_vector_store()
        store_count = store.count()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"向量索引不可用（{backend}）：{exc}"}

    from app.domain.models.knowledge_item import KnowledgeItem

    try:
        db_count = len(
            session.scalars(
                text("SELECT id FROM knowledge_items WHERE is_deleted = 0")
            ).all()
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"向量索引（{backend}）{store_count} 条；条目数查询失败：{exc}"}

    detail = f"后端 {backend}，索引 {store_count} 条 / 条目 {db_count} 条"
    if store_count is None:
        detail += "（该后端不支持计数，跳过一致性比对）"
        return {"ok": True, "detail": detail}
    if store_count < db_count:
        return {
            "ok": False,
            "detail": f"{detail}；索引缺条目，冷启动回填未生效或索引漂移，可重启进程重建",
        }
    return {"ok": True, "detail": detail}


def _check_embedding_freshness(session: Session) -> dict:
    """陈旧向量：模型标识与当前配置不一致（或未知）的已向量化条目数。"""
    try:
        embedding = build_embedding()
        from sqlalchemy import select

        from app.domain.models.knowledge_item import KnowledgeItem

        rows = session.scalars(
            select(KnowledgeItem.embedding_model).where(
                KnowledgeItem.is_deleted.is_(False),
                KnowledgeItem.embed_status == "embedded",
            )
        ).all()
        stale = sum(1 for name in rows if name != embedding.name)
        return {
            "ok": stale == 0,
            "detail": (
                f"当前模型 {embedding.name}；陈旧向量 {stale} 条"
                + ("" if stale == 0 else "，跑 scripts/rebuild_embeddings.py 重建")
            ),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"陈旧向量检查失败：{exc}"}


@router.get("/doctor")
def doctor(
    user_id: str = Depends(get_user_id),
    session: Session = Depends(get_session),
) -> dict:
    checks = {
        "database": _check_database(session),
        "llm": _check_llm(),
        "embedding": _check_embedding(),
        "vector_store": _check_vector_store(session),
        "embedding_freshness": _check_embedding_freshness(session),
    }
    status = "ok" if all(c["ok"] for c in checks.values()) else "degraded"
    return {"status": status, "checks": checks}
