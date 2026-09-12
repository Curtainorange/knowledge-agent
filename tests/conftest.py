"""测试夹具：临时 SQLite + 内存引擎 + MockProvider，全程不触发真实 API。

按测试铁律，禁止调用真实 DeepSeek；通过 settings.model_provider 空 KEY 保证
网关路由到 MockProvider。
"""
from __future__ import annotations

import tempfile

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
# 书籍文件写到临时目录，避免污染真实 data/books
settings.books_dir = tempfile.mkdtemp(prefix="cc_test_books_")
# 鉴权：固定密钥（避免随机密钥带来的不可预期），并调低 PBKDF2 迭代数以免拖慢测试
settings.jwt_secret = "pytest-fixed-secret-not-for-production"
settings.password_pbkdf2_iterations = 1000
# 限流：账号维度调小便于断言；IP 维度放大，避免测试客户端同一 IP 跨用例累计导致误伤
settings.login_max_attempts = 3
settings.login_ip_max_attempts = 100000
settings.login_window_seconds = 300
settings.login_lock_seconds = 300

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


def _reset_tables() -> None:
    """清空所有表。

    service 层现在会在内部 commit（先落库再补向量，避免长时间持有 SQLite 写锁），
    因此数据不再随事务回滚消失。使用 session 夹具的用例必须显式重置，否则相互污染。
    """
    with _engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture()
def session():
    _reset_tables()
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