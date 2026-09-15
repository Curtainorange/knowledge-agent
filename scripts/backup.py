"""一键备份：把 dev.db 与 data/（上传的书籍与插图）备份到 backups/backup_<时间戳>/。

用法::

    python scripts/backup.py            # 备份，保留最近 10 份
    python scripts/backup.py --keep 20  # 保留最近 20 份
    python scripts/backup.py --keep 0   # 备份但不清理旧份
    python scripts/backup.py --list     # 只列出已有备份

**为什么是脚本而不是接口**：备份是全库级操作（跨所有用户），不该由某个登录用户触发；
放在 scripts/ 下，只有拿到机器权限的人才能备份。

**为什么用 sqlite3 backup API 而不是直接复制文件**：服务器运行中时数据库可能正在写入，
直接 `shutil.copy` 可能拿到不一致的快照；SQLite 的在线备份 API 能保证一致性。
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "dev.db"
DATA_DIR = ROOT / "data"
BACKUP_ROOT = ROOT / "backups"


def _fmt_size(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def list_backups() -> None:
    if not BACKUP_ROOT.is_dir():
        print("还没有任何备份。")
        return
    backups = sorted(BACKUP_ROOT.glob("backup_*"))
    if not backups:
        print("还没有任何备份。")
        return
    print(f"共 {len(backups)} 份备份（{BACKUP_ROOT}）：")
    for b in backups:
        db = b / "dev.db"
        size = db.stat().st_size if db.exists() else 0
        print(f"  {b.name}   {_fmt_size(size)}")


def make_backup(keep: int) -> int:
    if not DB_PATH.exists():
        print(f"找不到数据库文件：{DB_PATH}", file=sys.stderr)
        return 1

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = BACKUP_ROOT / f"backup_{stamp}"
    target.mkdir(parents=True, exist_ok=True)

    # 数据库：用在线备份 API，服务器运行中也能拿到一致快照
    src = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(target / "dev.db")
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    db_size = (target / "dev.db").stat().st_size
    file_count = 0
    if DATA_DIR.is_dir():
        shutil.copytree(DATA_DIR, target / "data")
        file_count = sum(1 for p in (target / "data").rglob("*") if p.is_file())

    print(f"备份完成：{target}")
    print(f"  数据库 {_fmt_size(db_size)}；书籍/插图文件 {file_count} 个")

    if keep > 0:
        for old in sorted(BACKUP_ROOT.glob("backup_*"))[:-keep]:
            try:
                shutil.rmtree(old)
                print(f"  已清理旧备份：{old.name}")
            except OSError as exc:  # 环境可能装了 safe-delete 钩子
                print(f"  旧备份清理失败（可手动删除）：{old.name} — {exc}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="备份 dev.db 与 data/")
    parser.add_argument("--keep", type=int, default=10, help="保留最近 N 份（默认 10，0 表示不清理）")
    parser.add_argument("--list", action="store_true", help="只列出已有备份")
    args = parser.parse_args()

    if args.list:
        list_backups()
        return 0
    return make_backup(args.keep)


if __name__ == "__main__":
    raise SystemExit(main())
