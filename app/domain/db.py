"""数据库引擎与会话工厂（访问层不能直接谈，须经仓储）。"""
from __future__ import annotations

import logging

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.domain.models.base import Base

logger = logging.getLogger(__name__)

_connect_args: dict = {}
if settings.database_url.startswith("sqlite"):
    _connect_args["check_same_thread"] = False
    # 并发写时先等待而不是立刻抛 "database is locked"（默认只等 5 秒）
    _connect_args["timeout"] = 30.0

engine = create_engine(settings.database_url, connect_args=_connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def _scalar_server_default(column) -> str | None:
    """从列定义推断一个可安全写入 DDL 的标量默认值（仅数字 / 字符串 / 时间戳）。"""
    if column.server_default is not None:
        arg = column.server_default.arg
        if isinstance(arg, str):
            return arg
        # func.now() / CURRENT_TIMESTAMP → SQLite 可用 CURRENT_TIMESTAMP
        name = getattr(arg, "name", None) or str(arg).lower()
        if name in ("now", "now()", "current_timestamp"):
            return "CURRENT_TIMESTAMP"
    default = getattr(column, "default", None)
    if default is not None and getattr(default, "is_scalar", False):
        arg = default.arg
        if isinstance(arg, bool):
            return "1" if arg else "0"
        if isinstance(arg, (int, float)):
            return str(arg)
        if isinstance(arg, str):
            return "'" + arg.replace("'", "''") + "'"
    return None


def _ensure_sqlite_columns(engine_=None) -> None:
    """开发环境 schema 自愈：补上已有表缺失的列。

    `create_all` 只会创建**缺失的表**，不会给**已存在的表**加新列。P0 用 SQLite +
    `create_all` 快速迭代，一旦给模型加了新列（如 users.token_version、
    cost_logs.cached_tokens），老库会因 `no such column` 在运行时 500。这里在启动时
    对比模型列与实际列，自动 `ALTER TABLE ... ADD COLUMN` 补齐——只加缺失列，
    不改名、不改类型、不删列（那些仍需走 alembic 迁移）。
    """
    if not settings.database_url.startswith("sqlite"):
        return  # PostgreSQL 走 alembic 迁移，不在此处理

    eng = engine_ or engine
    insp = inspect(eng)
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        existing = {c["name"] for c in insp.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            col_type = column.type.compile(dialect=eng.dialect)
            default = _scalar_server_default(column)
            # NOT NULL 且无默认值的新列无法安全补（SQLite 不允许），跳过留给 alembic
            if not column.nullable and default is None:
                logger.warning(
                    "sqlite schema drift: %s.%s 是 NOT NULL 且无默认值，跳过自愈（需 alembic）",
                    table.name, column.name,
                )
                continue
            ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}'
            if default is not None:
                ddl += f" DEFAULT {default}"
            with eng.begin() as conn:
                conn.execute(text(ddl))
            logger.warning(
                "sqlite schema drift: 已为 %s 表补列 %s（%s）",
                table.name, column.name, col_type,
            )


def init_db() -> None:
    """建表 + SQLite 开发库缺列自愈。

    P0 用 `create_all` 快速建表；接入 PostgreSQL 后切 Alembic 迁移管理版本。
    `create_all` 之后补一步 `_ensure_sqlite_columns`，避免「模型加了新列、老库
    缺列导致运行时 500」——这也是本次「三个页面打不开」的直接根因。
    """
    from app.domain import models  # noqa: F401  确保所有模型注册到 Base.metadata
    Base.metadata.create_all(bind=engine)
    _ensure_sqlite_columns()
