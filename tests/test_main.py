"""app.main 的启动行为：建库挪进 lifespan，库坏不崩。"""
from __future__ import annotations

from fastapi.testclient import TestClient

import app.main as main_module


def test_lifespan_calls_init_db(monkeypatch):
    """init_db 应在 lifespan 里执行（而不是 import 期就建库）。"""
    calls = {"n": 0}

    def fake_init_db():
        calls["n"] += 1

    monkeypatch.setattr(main_module.db, "init_db", fake_init_db)
    with TestClient(main_module.app) as c:
        assert c.get("/health").status_code == 200
    assert calls["n"] == 1


def test_init_db_failure_keeps_app_alive(monkeypatch):
    """库损坏/被锁时 init_db 抛异常，进程仍应起、health 仍 200。

    这是 2026-11-09 崩溃面复审落下的待办：以前 init_db 在 import 期执行，
    库一坏进程直接起不来（日志只剩裸 traceback）。挪进 lifespan + try/except 后，
    失败只打日志、进程照常启动，watchdog 不会因此陷入重启死循环。
    """

    def boom():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(main_module.db, "init_db", boom)
    with TestClient(main_module.app) as c:
        assert c.get("/health").status_code == 200
