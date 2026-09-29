"""真实 L2 扫描验证：影子主张（通读笔记）能否与用户笔记/其他书撞出冲突。

连 dev.db 走真实 LLM KEY；只做 scan（增量、幂等），打印结果与新增冲突的视图。
用法：python scripts/run_l2_scan.py
"""
from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.config import settings


def main() -> None:
    from app.agent.conflict_view import conflict_views
    from app.agent.l2_orchestrator import L2Orchestrator
    from app.domain.models.user import User
    from app.llm.gateway import ModelGateway

    engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})
    session = sessionmaker(bind=engine)()

    # 取「有通读笔记」的那个用户（read_demo_book 通读的书都挂在他名下）
    user = session.scalars(
        select(User).where(User.id.in_(
            session.execute(
                __import__("sqlalchemy").text("SELECT DISTINCT user_id FROM book_agent_readings")
            ).scalars()
        ))
    ).first()
    if user is None:
        raise SystemExit("没有任何通读记录，先跑 scripts/read_demo_book.py")

    print(f"user={user.id}")
    gateway = ModelGateway()
    print("providers:", gateway.real_provider_names or ["Mock"], flush=True)

    result = L2Orchestrator(gateway, session).scan(user_id=user.id)
    session.commit()

    print({
        "scanned_items": result.scanned_items,
        "claims_extracted": result.claims_extracted,
        "pairs_judged": result.pairs_judged,
        "conflicts_found": result.conflicts_found,
        "conflicts_suppressed": result.conflicts_suppressed,
        "book_readings_used": result.book_readings_used,
    })

    if result.conflict_ids:
        views = conflict_views(session, user_id=user.id, conflict_ids=result.conflict_ids)
        for v in views:
            print("\n---")
            print(f"[{v['conflict_type']} conf={round(float(v['confidence']), 2)}] "
                  f"{v['title_a']} ↔ {v['title_b']}")
            print(f"A: {v['claim_a']}")
            print(f"B: {v['claim_b']}")
            print(f"依据: {v['detail']}")
            print(f"建议: {v['suggestion']}")
    else:
        print("（本轮没有新的矛盾入库——检查书来源主张是否真的进了漏斗）")


if __name__ == "__main__":
    main()
