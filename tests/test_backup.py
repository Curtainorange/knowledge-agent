"""备份保留策略测试（scripts/backup.py 的 make_backup / 旧份清理）。"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from datetime import datetime as real_datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "backup.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("backup_script_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules.pop("backup_script_under_test", None)
    spec.loader.exec_module(module)
    return module


class _FakeClock:
    """每次 now() 前进一分钟，保证同一秒内多次备份得到不同的时间戳目录。"""

    def __init__(self, start: real_datetime):
        self._current = start

    def now(self) -> real_datetime:
        self._current = real_datetime(
            self._current.year, self._current.month, self._current.day,
            self._current.hour, self._current.minute + 1, 0,
        )
        return self._current

    def strftime(self, fmt: str) -> str:  # pragma: no cover - backup.py 不直接用它
        return self._current.strftime(fmt)


@pytest.fixture()
def backup_env(tmp_path, monkeypatch):
    """把脚本的路径常量指到临时目录，造一个可用 SQLite 库 + data 目录。"""
    module = _load_module()
    db_path = tmp_path / "dev.db"
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
    conn.close()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "a.txt").write_text("hello", encoding="utf-8")
    backup_root = tmp_path / "backups"

    monkeypatch.setattr(module, "DB_PATH", db_path)
    monkeypatch.setattr(module, "DATA_DIR", data_dir)
    monkeypatch.setattr(module, "BACKUP_ROOT", backup_root)
    return module, backup_root, db_path


def test_make_backup_creates_snapshot(backup_env):
    module, backup_root, db_path = backup_env
    module.datetime = _FakeClock(real_datetime(2026, 9, 23, 10, 0, 0))
    assert module.make_backup(keep=0) == 0
    backups = sorted(backup_root.glob("backup_*"))
    assert len(backups) == 1
    # 快照里的库内容一致
    check = sqlite3.connect(backups[0] / "dev.db")
    assert check.execute("SELECT x FROM t").fetchone() == (1,)
    check.close()
    # data/ 一并拷贝
    assert (backups[0] / "data" / "a.txt").exists()


def test_retention_keeps_latest_n(backup_env):
    module, backup_root, _ = backup_env
    clock = _FakeClock(real_datetime(2026, 9, 23, 10, 0, 0))
    module.datetime = clock
    for _ in range(5):
        module.make_backup(keep=3)
    backups = sorted(backup_root.glob("backup_*"))
    assert len(backups) == 3
    # 留下的是最近 3 份（时间戳单调递增，sorted 后取尾部）
    assert backups[-1] == max(backup_root.glob("backup_*"), key=lambda p: p.name)


def test_keep_zero_never_cleans(backup_env):
    module, backup_root, _ = backup_env
    module.datetime = _FakeClock(real_datetime(2026, 9, 23, 10, 0, 0))
    for _ in range(4):
        module.make_backup(keep=0)
    assert len(list(backup_root.glob("backup_*"))) == 4


def test_missing_db_returns_error(backup_env, monkeypatch):
    module, _, _ = backup_env
    monkeypatch.setattr(module, "DB_PATH", module.DB_PATH.parent / "nope.db")
    assert module.make_backup(keep=3) == 1
