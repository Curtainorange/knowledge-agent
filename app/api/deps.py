"""FastAPI 依赖注入：会话 / 网关 / orchestrator。

P0 单用户演示：user_id 取自头部 X-User-Id，缺省回落 "demo-user"。
正式鉴权（JWT → 注入 user_id）见需求 9.3.1，后续接入。
"""
from __future__ import annotations

from fastapi import Header

from app.domain.db import SessionLocal
from app.llm.gateway import ModelGateway


def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def get_gateway() -> ModelGateway:
    # 应用级单例；provider 按 settings 选择（有 KEY→DeepSeek，否则 Mock）
    return ModelGateway()


def get_user_id(x_user_id: str = Header(default="demo-user", alias="X-User-Id")) -> str:
    return x_user_id