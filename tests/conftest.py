"""测试夹具：临时 SQLite + 内存引擎 + MockProvider，全程不触发真实 API。

按测试铁律，禁止调用真实 DeepSeek；通过 settings.model_provider 空 KEY 保证
网关路由到 MockProvider。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import deps
from app.core.config import settings
from app.domain.models.base import Base
from app.main import app

# 测试强制走 Mock（即便本机配了 .env）
settings.deepseek_api_key = ""
# 测试强制走确定性哈希 embedding：全程不触网、不装大模型（铁律）
settings.embedding_backend = "hash"

# 覆盖 DATABASE_URL，使用内存 SQLite（StaticPool 共享同一连接）
settings.database_url = "sqlite://"
_engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
    future=True,
)
Base.metadata.create_all(_engine)
_TestingSession = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False, future=True)


@pytest.fixture()
def session():
    s = _TestingSession()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture(scope="session")
def client():
    def override_session():
        s = _TestingSession()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[deps.get_session] = override_session
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()