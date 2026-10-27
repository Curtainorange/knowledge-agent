"""Loop 入口：把「人工跑一遍通读」变成可断点续跑、会自己停的轮次循环。

原来每读一本书，人要手动做这些步（现在每一步都对应一条命令，见 --help）：
1. 查书架找没读过的书；2. 判断这本书有没有正文（扫描版/占位要跳过）；
3. 跑 `read_demo_book.py <书名>`（按标题模糊匹配，中文书名得打全）；
4. 超长书要反复跑 `--resume` 直到读完；5. 人工看输出判断 ok / degraded；
6. 失败的书手工标删；7. 写进 dev_logs。

用法：
    python scripts/run_loop.py --plan                    # 只看这轮会做什么，不动状态
    python scripts/run_loop.py                           # 跑（默认 5 轮，连续 3 次失败熔断）
    python scripts/run_loop.py --rounds 1                # 只跑一轮
    python scripts/run_loop.py --book-id <id> --retry     # 失败重跑某一本（清零重试与断点）
    python scripts/run_loop.py --json                    # 结构化汇总

状态库在 `runtime/loop_state.db`（独立文件，不碰 dev.db 的表结构），删掉即重置。
退出码：0 正常结束 / 2 熔断（需要人看一眼）/ 1 参数或启动错误。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_STATE = ROOT / "runtime" / "loop_state.db"


def _parse() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="书籍通读 loop：状态队列 + 独立验证 + 停止规则",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "可复用的三步：\n"
            "  1) --plan 先看一眼会跑什么（不消耗任务、不调模型）\n"
            "  2) 直接跑，等它自己停（轮次上限 / 队列空 / 连续 3 次失败熔断）\n"
            "  3) 看每一行 [loop] 报告：task / 耗时 / tokens / verdict\n"
        ),
    )
    p.add_argument("--rounds", type=int, default=5, help="单次最多跑几轮（默认 5）")
    p.add_argument("--plan", action="store_true", help="只打印计划，不动状态、不调模型")
    p.add_argument("--book-id", dest="book_ids", action="append", default=[],
                   help="只处理这些书（可重复）")
    p.add_argument("--retry", action="store_true", help="配合 --book-id：清零重试次数与断点")
    p.add_argument("--force", action="store_true", help="预检不通过也强跑（默认跳过）")
    p.add_argument("--state", default=str(DEFAULT_STATE), help="状态库路径")
    p.add_argument("--json", action="store_true", help="输出结构化汇总")
    return p.parse_args()


def main() -> int:
    args = _parse()

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.core.config import settings
    from app.domain.models.book import Book
    from app.loop.runner import StopRules, book_key, build_queue, run_loop
    from app.loop.state import LoopStore
    from app.loop.verify import precheck

    engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})
    session = sessionmaker(bind=engine)()
    store = LoopStore(Path(args.state))
    store.reclaim_running()  # 上次崩在 running 的任务先捡回来（断点保留）

    if args.book_ids:
        for bid in args.book_ids:
            book = session.get(Book, bid)
            if book is None:
                print(f"[loop] 没有这本书：{bid}", file=sys.stderr)
                return 1
            reason = precheck(book)
            if reason and not args.force:
                print(f"[loop] 跳过《{book.title}》：{reason}（要强跑加 --force）")
                continue
            store.enqueue(book_key(book.id), book.title)
            # --force / --retry 都要显式重置：任务可能已经是 skipped（被预检跳过）
            # 或 dead / done，只 enqueue 是改不动已有状态的（INSERT OR IGNORE）
            if args.retry or args.force:
                store.reset_task(book_key(book.id))
                print(f"[loop] 已重置《{book.title}》（attempts=0, read_upto=0）")
    else:
        # --plan 也要建队列：否则第一次用等于什么都没看到。入队是幂等的，不消耗任务。
        stats = build_queue(store, session)
        print(
            f"[loop] 队列：新增 {stats.queued}、预检跳过 {stats.skipped}、"
            f"已读完 {stats.already_done}"
        )

    gateway = None
    if not args.plan:
        from app.llm.gateway import ModelGateway

        gateway = ModelGateway()
        print(f"[loop] 模型链：{gateway.real_provider_names or ['MockProvider（未配 KEY）']}")

    summary = run_loop(
        store, session, gateway=gateway,
        rules=StopRules(max_rounds=args.rounds, max_consecutive_failures=3),
        dry_run=args.plan,
        only_keys={book_key(b) for b in args.book_ids} or None,
    )
    session.close()

    totals = summary.totals()
    tokens = int(totals["prompt_tokens"] + totals["completion_tokens"])
    print(
        f"[loop] 结束：stopped={summary.stopped_reason} rounds={totals['rounds']} "
        f"ok={totals['ok']} 用时 {totals['elapsed_ms'] / 1000:.1f}s tokens={tokens}"
        f"({int(totals['prompt_tokens'])}+{int(totals['completion_tokens'])},"
        f"cache{int(totals['cached_tokens'])}) ¥{totals['cost']:.4f} "
        f"连续失败={summary.consecutive_failures}"
    )
    print(f"[loop] 队列状态：{store.stats()}")

    if args.json:
        print(json.dumps(
            {"stopped_reason": summary.stopped_reason, "totals": totals,
             "consecutive_failures": summary.consecutive_failures, "queue": store.stats()},
            ensure_ascii=False,
        ))

    return 2 if summary.stopped_reason == "circuit_open" else 0


if __name__ == "__main__":
    sys.exit(main())
