"""数据库初始化测试：SQLite 开发库 schema 漂移自愈。

`create_all` 只建缺失的表、不给已有表加列——这是「模型加了新列、老库运行时
`no such column` 500」的根因（本次三个页面打不开的直接原因）。这里验证自愈逻辑
能在启动时自动 `ALTER TABLE ... ADD COLUMN` 补齐缺失列。
"""
from __future__ import annotations

from sqlalchemy import create_engine, inspect, text

from app.domain.db import _ensure_sqlite_columns, _scalar_server_default
from app.domain.models.base import Base
from app.domain.models.user import User


def test_scalar_server_default_inference():
    """从列定义推断 DDL 默认值：整数 / 字符串 / func.now() 三类。"""
    assert _scalar_server_default(User.__table__.c.token_version) == "0"
    assert _scalar_server_default(User.__table__.c.push_frequency) == "'weekly'"
    # created_at 的 server_default=func.now() → CURRENT_TIMESTAMP
    assert _scalar_server_default(User.__table__.c.created_at) == "CURRENT_TIMESTAMP"


def test_sqlite_column_drift_self_heal(tmp_path):
    """老库缺列 → 自愈补列（不改名、不动已有列）。"""
    db_path = tmp_path / "drift.db"
    eng = create_engine(f"sqlite:///{db_path}", future=True)

    # 造一个「旧版 users」：有基础列 + 时间戳，缺 token_version / deleted_at
    with eng.begin() as conn:
        conn.execute(text(
            "CREATE TABLE users ("
            "id VARCHAR(36) PRIMARY KEY, "
            "username VARCHAR(64), "
            "password_hash VARCHAR(255), "
            "nickname VARCHAR(64), "
            "push_frequency VARCHAR(16), "
            "knowledge_credentials TEXT, "
            "created_at DATETIME, "
            "updated_at DATETIME)"
        ))

    _ensure_sqlite_columns(engine_=eng)

    cols = {c["name"] for c in inspect(eng).get_columns("users")}
    assert "token_version" in cols
    assert "deleted_at" in cols
    # 已有列不受影响
    assert "username" in cols and "created_at" in cols


def test_sqlite_self_heal_is_idempotent(tmp_path):
    """自愈是幂等的：列已补齐时再跑一次不报错、不加重复列。"""
    db_path = tmp_path / "idem.db"
    eng = create_engine(f"sqlite:///{db_path}", future=True)
    with eng.begin() as conn:
        conn.execute(text(
            "CREATE TABLE users ("
            "id VARCHAR(36) PRIMARY KEY, "
            "username VARCHAR(64), "
            "password_hash VARCHAR(255), "
            "nickname VARCHAR(64), "
            "created_at DATETIME, "
            "updated_at DATETIME)"
        ))

    _ensure_sqlite_columns(engine_=eng)
    first = {c["name"] for c in inspect(eng).get_columns("users")}
    _ensure_sqlite_columns(engine_=eng)  # 第二次不应报错
    second = {c["name"] for c in inspect(eng).get_columns("users")}
    assert first == second


def test_metadata_registers_all_expected_tables():
    """三张 L5/推送新表必须注册到 metadata（否则 create_all 建不出来）。"""
    names = {t.name for t in Base.metadata.sorted_tables}
    assert {"push_jobs", "push_logs", "cognitive_diagnoses", "users", "claims"} <= names
