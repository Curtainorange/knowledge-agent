"""数据库引擎与会话工厂（访问层不能直接谈，须经仓储）。"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.domain.models.base import Base

_connect_args: dict = {}
if settings.database_url.startswith("sqlite"):
    _connect_args["check_same_thread"] = False

engine = create_engine(settings.database_url, connect_args=_connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def init_db() -> None:
    """建表（P0 用 create_all；后续切 Alembic 迁移管理版本）。"""
    from app.domain import models  # noqa: F401  确保所有模型注册到 Base.metadata
    Base.metadata.create_all(bind=engine)