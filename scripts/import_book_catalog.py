"""把开源书目（ebook-treasure-chest）导入成本地离线目录。

为什么是「离线导入」而不是运行时查询：
那份数据本身就是个静态 JSON，运行时去拉它只是把一次本地查找换成一次网络请求
——多一个会挂的依赖、多一份延迟、还要求服务器能连 GitHub。所以约定是
**手动跑这个脚本，产物落盘，运行时只读文件**。

数据事实（2026-11-18 实测）：24,071 行，其中 24,002 行挂在 `url89.ctfile.com`
（城通网盘，带分享密码），另 69 行链接字段是字面量「链接未找到」。字段为
title / author / link / category / language / level / formats。这里只留用得上四个
+ formats，`language` / `level` 全部是 Unknown 级别的占位值，留着只让文件更大。

**行 ≠ 书**：源仓库的 `md/` 按标签组织，一本书挂几个标签就出现几行。24,071 行合并后
是 11,348 本，每本平均 2.2 个标签（1 个 2,457 本 / 2 个 5,440 / 3 个 3,156 / 4 个 280）。
这些行不是脏数据，差别就在标签上——所以**合并、标签取并集**，
按「书名+作者+链接」去重会把标签静默删掉。

用法：

    python scripts/import_book_catalog.py                # 从 GitHub 拉最新
    python scripts/import_book_catalog.py --file x.json  # 从本地已有文件导入（离线/内网）

导入后统计会打印：行数 → 书数、标签总数与分布、含 epub 的本数。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SOURCE_URL = "https://raw.githubusercontent.com/jbiaojerry/ebook-treasure-chest/main/docs/all-books.json"
DEFAULT_OUT = ROOT / "data" / "book_catalog.json"

# 只留这些字段。少一个字段，24k 条的文件小一半。
_KEEP = ("title", "author", "link", "categories", "formats")
_MISSING_LINK = "链接未找到"
_TIMEOUT = 60


def _download(url: str) -> list:
    request = urllib.request.Request(url, headers={"User-Agent": "cognitive-copilot-import"})
    with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
        payload = response.read()
    return json.loads(payload.decode("utf-8"))


def _tags(item: dict) -> list[str]:
    raw = item.get("categories")
    if not isinstance(raw, list):
        raw = [item.get("category")] if item.get("category") else []
    out: list[str] = []
    for tag in raw:
        text = str(tag or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _formats(item: dict) -> list[str]:
    raw = item.get("formats")
    if not isinstance(raw, list):
        return []
    return [str(f).strip() for f in raw if str(f).strip()]


def _slim(raw: list) -> tuple[list[dict], dict]:
    """按链接合并 + 精简 + 统计。

    **合并而不是去重**：源仓库的 `md/` 按标签组织，一本书挂几个标签就出现几行。
    这些行不是脏数据，它们的差别恰恰是标签——按「书名+作者+链接」去重会把标签静默删掉
    （实测每本平均丢 1.2 个）。所以这里按链接聚合，标签取并集。
    """
    stats = {"raw": len(raw), "no_link": 0, "merged": 0}
    slots: dict[str, dict] = {}
    order: list[str] = []

    for item in raw:
        if not isinstance(item, dict):
            continue
        link = str(item.get("link") or "").strip()
        if not link or link == _MISSING_LINK:
            stats["no_link"] += 1
            continue
        title = str(item.get("title") or "").strip()
        author = str(item.get("author") or "").strip()

        slot = slots.get(link)
        if slot is None:
            slots[link] = {
                "title": title,
                "author": author,
                "link": link,
                "categories": _tags(item),
                "formats": _formats(item),
            }
            order.append(link)
            continue

        stats["merged"] += 1
        if not slot["title"] and title:
            slot["title"] = title
        if not slot["author"] and author:
            slot["author"] = author
        for tag in _tags(item):
            if tag not in slot["categories"]:
                slot["categories"].append(tag)
        for fmt in _formats(item):
            if fmt not in slot["formats"]:
                slot["formats"].append(fmt)

    entries = [slots[link] for link in order if slots[link]["title"]]
    stats["kept"] = len(entries)
    stats["tags"] = sum(len(e["categories"]) for e in entries)
    stats["tag_dist"] = dict(sorted(Counter(len(e["categories"]) for e in entries).items()))
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
        f"原始 {stats['raw']} 条 → 合并为 {stats['kept']} 本"
        f"（并掉 {stats['merged']} 条同书多标签的行 / 无链接 {stats['no_link']}）"
    )
    dist = " / ".join(f"{n} 个 {c} 本" for n, c in stats["tag_dist"].items())
    print(f"标签共 {stats['tags']} 个，平均 {stats['tags'] / max(stats['kept'], 1):.1f} 个/本（{dist}）")
    print(f"其中有 epub 的 {stats['epub']} 本")
    print(f"写入：{args.out}（{size_mb:.1f} MB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
