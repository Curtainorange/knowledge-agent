"""把开源书目（ebook-treasure-chest）导入成本地离线目录。

为什么是「离线导入」而不是运行时查询：
那份数据本身就是个静态 JSON，运行时去拉它只是把一次本地查找换成一次网络请求
——多一个会挂的依赖、多一份延迟、还要求服务器能连 GitHub。所以约定是
**手动跑这个脚本，产物落盘，运行时只读文件**。

数据事实（2026-11-18 实测）：24,071 条，其中 24,002 条挂在 `url89.ctfile.com`
（城通网盘，带分享密码），另 69 条链接字段是字面量「链接未找到」。字段为
title / author / link / category / language / level / formats。这里只留前五个里用得上
的四个 + formats，`language` / `level` 全部是 Unknown 级别的占位值，留着只是让文件更大。

用法：

    python scripts/import_book_catalog.py                # 从 GitHub 拉最新
    python scripts/import_book_catalog.py --file x.json  # 从本地已有文件导入（离线/内网）

导入后统计会打印出来：总条数 / 去重后 / 含 epub 的条数。**含 epub 那个数字是重点**——
上传白名单只认 .txt/.epub/.pdf，只有 mobi/azw3 的条目对阅读器无效。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SOURCE_URL = "https://raw.githubusercontent.com/jbiaojerry/ebook-treasure-chest/main/docs/all-books.json"
DEFAULT_OUT = ROOT / "data" / "book_catalog.json"

# 只留这些字段。少一个字段，24k 条的文件小一半。
_KEEP = ("title", "author", "link", "category", "formats")
_MISSING_LINK = "链接未找到"
_TIMEOUT = 60


def _download(url: str) -> list:
    request = urllib.request.Request(url, headers={"User-Agent": "cognitive-copilot-import"})
    with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
        payload = response.read()
    return json.loads(payload.decode("utf-8"))


def _slim(raw: list) -> tuple[list[dict], dict[str, int]]:
    """精简 + 去重 + 统计。返回 (条目, 计数)，计数用于打印给人看。"""
    stats = {"raw": len(raw), "no_title": 0, "no_link": 0, "duplicate": 0}
    seen: set[tuple[str, str, str]] = set()
    entries: list[dict] = []

    for item in raw:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        link = str(item.get("link") or "").strip()
        if not title:
            stats["no_title"] += 1
            continue
        if not link or link == _MISSING_LINK:
            stats["no_link"] += 1
            continue

        author = str(item.get("author") or "").strip()
        key = (title, author, link)
        if key in seen:
            stats["duplicate"] += 1
            continue
        seen.add(key)

        formats = item.get("formats")
        if not isinstance(formats, list):
            formats = []
        entries.append(
            {
                "title": title,
                "author": author,
                "link": link,
                "category": str(item.get("category") or "").strip(),
                "formats": [str(f).strip() for f in formats if str(f).strip()],
            }
        )

    stats["kept"] = len(entries)
    stats["epub"] = sum(1 for e in entries if any(f.upper() == "EPUB" for f in e["formats"]))
    return entries, stats


def main() -> int:
    parser = argparse.ArgumentParser(description="导入开源书目到本地离线目录")
    parser.add_argument("--url", default=SOURCE_URL, help="来源地址（默认仓库 raw 地址）")
    parser.add_argument("--file", type=Path, help="改从本地 JSON 导入，不联网")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="输出路径")
    args = parser.parse_args()

    if args.file:
        print(f"从本地文件导入：{args.file}")
        try:
            raw = json.loads(args.file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"读取失败：{exc}", file=sys.stderr)
            return 1
    else:
        print(f"拉取：{args.url}")
        try:
            raw = _download(args.url)
        except (urllib.error.URLError, ValueError) as exc:
            print(f"下载失败：{exc}", file=sys.stderr)
            print("网络不通时可用 --file 从已下载的 all-books.json 导入。", file=sys.stderr)
            return 1

    if not isinstance(raw, list):
        print(f"数据形状不对：期望 list，实际 {type(raw).__name__}", file=sys.stderr)
        return 1

    entries, stats = _slim(raw)
    if not entries:
        print("没有任何可用条目，已中止（不覆盖现有文件）。", file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    # ensure_ascii=False：书名是中文，转义后文件大一倍且不可读；
    # separators 去掉多余空格，7MB 的文件能省下两成。
    args.out.write_text(
        json.dumps(entries, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    size_mb = args.out.stat().st_size / (1024 * 1024)
    print(
        f"原始 {stats['raw']} 条 → 保留 {stats['kept']} 条"
        f"（无书名 {stats['no_title']} / 无链接 {stats['no_link']} / 重复 {stats['duplicate']}）"
    )
    print(f"其中有 epub 的 {stats['epub']} 条（只有 epub 能进阅读器）")
    print(f"写入：{args.out}（{size_mb:.1f} MB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
