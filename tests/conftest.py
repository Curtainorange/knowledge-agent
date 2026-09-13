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
# 注册限流：测试里会有大量注册（helpers.sign_in 每个用例都注册），阈值放大；
# 专门验证限流的用例用 monkeypatch 调小并重置限流器
settings.register_max_attempts_per_ip = 100000

# 后台 worker：测试里不起轮询线程（避免后台写库干扰断言），改为用例显式调 run_once；
# 重试退避设为 0，便于在一个用例内验证「失败 → 重试 → 成功 / 死信」
settings.worker_enabled = False
settings.task_retry_backoff_seconds = 0.0

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


@pytest.fixture(autouse=True)
def _reset_throttles():
    """每个用例前后都重建限流器。

    限流器是模块级惰性单例：某个用例把阈值改小（monkeypatch）后，重建出来的实例
    会带着小阈值继续影响后续用例——表现为"单独跑过、一起跑就挂"。这里统一隔离。
    """
    from app.api.endpoints import auth as auth_module

    auth_module.reset_login_throttle()
    yield
    auth_module.reset_login_throttle()


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