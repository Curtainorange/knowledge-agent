"""L2 真实链路冒烟：真实模型完整跑通「录入 → 主张提取 → 冲突判定 → 反馈」。

用法（在项目根目录；key 写在 .env 里即可，不必在这里传环境变量）：
    python scripts/smoke_l2.py

- 使用独立临时库 smoke_l2_tmp.db（*.db 已在 .gitignore），不污染 dev.db；
  跑完可手动删除。
- 供应商自动跟随后端配置（MIMO_API_KEY 优先，其次 DEEPSEEK_API_KEY）；
  两个都没配则网关路由到 MockProvider，只能验证链路连通性、验证不了真实判定
  质量——脚本会显式失败提醒。
"""
from __future__ import annotations

import os
import sys
import uuid

# 必须在导入 app 之前设置：配置在 import 时读取环境变量
os.environ.setdefault("DATABASE_URL", "sqlite:///./smoke_l2_tmp.db")
try:  # 每次冒烟用全新临时库，避免上次运行的数据干扰断言
    os.remove("smoke_l2_tmp.db")
except FileNotFoundError:
    pass

from fastapi.testclient import TestClient  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.main import app  # noqa: E402


def main() -> int:
    ok = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        print(f"[{'OK  ' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))
        if not cond:
            ok = False

    client = TestClient(app)
    # 供应商无关：MiMo / DeepSeek 都算「真实」，只有 Mock 时冒烟无意义。
    check("供应商为真实模型（非 Mock）", settings.model_provider != "mock",
          f"当前走 {settings.model_provider}（未配 MIMO/DEEPSEEK key 时走 Mock，冒烟无意义）"
          if settings.model_provider == "mock" else f"当前={settings.model_provider} / {settings.active_model}")

    # 1) 注册（随机用户名，幂等不依赖库状态）
    username = f"smoke_l2_{uuid.uuid4().hex[:8]}"
    r = client.post("/api/v1/auth/register", json={"username": username, "password": "smoke12345"})
    check("注册", r.status_code == 201)
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

    # 2) 录入两条对同一问题立场互斥的知识（读书方法：精读 vs 跳读）
    items = [
        {"title": "精读主义：逐字消化",
         "content": "读经典必须逐字逐句精读，跳读会漏掉承转启合的关键论证。"
                    "一本书的价值就在正文里，只有完整读完每一章才算真正读过，"
                    "只看目录和结论等于没读。"},
        {"title": "跳读主义：抓大放小",
         "content": "读书根本不必逐字读完。应该只读目录、章节开头和结论，"
                    "逐字精读是低效的自我感动——绝大多数书不值得完整读一遍，"
                    "能用 20% 的时间拿到核心观点才是聪明的读者。"},
    ]
    for it in items:
        r = client.post("/api/v1/knowledge/items", json=it, headers=headers)
        check(
            f"录入「{it['title']}」",
            r.status_code == 200,
            f"embed_status={r.json().get('embed_status', '?')}",
        )

    # 3) 触发 L2 扫描（真实模型调用：2 次提取 + N 次判定，需数十秒）
    print(f"\n→ 触发 L2 扫描（真实模型 {settings.active_model}，请稍候）...")
    r = client.post("/api/v1/l2/scan", headers=headers)
    check("POST /l2/scan", r.status_code == 200, r.text[:300] if r.status_code != 200 else "")
    body = r.json()
    print(
        f"   扫描摘要：扫描条目={body['scanned_items']} 提取主张={body['claims_extracted']} "
        f"判定对数={body['pairs_judged']} 发现冲突={body['conflicts_found']} "
        f"抑制={body['conflicts_suppressed']} 提取失败={body['extraction_failures']}"
    )

    # 4) 冲突列表 + 内容质量
    r = client.get("/api/v1/l2/conflicts", headers=headers)
    conflicts = r.json()["items"]
    for c in conflicts:
        print(f"\n   ⚡ [{c['conflict_type']}] {c['title_a']} ↔ {c['title_b']}（置信度 {c['confidence']}）")
        print(f"      主张A：{c['claim_a']}")
        print(f"      主张B：{c['claim_b']}")
        print(f"      依据：{c['detail']}")
        print(f"      建议：{c['suggestion']}")

    check("发现至少 1 处冲突", body["conflicts_found"] >= 1)
    if conflicts:
        c = conflicts[0]
        check("冲突内容完整（类型/双方主张/依据/建议）",
              bool(c["conflict_type"] and c["claim_a"] and c["claim_b"] and c["detail"]))
        # 5) 反馈闭环
        r = client.patch(
            f"/api/v1/l2/conflicts/{c['conflict_id']}/state",
            json={"state": "ignored"}, headers=headers,
        )
        check("反馈状态更新为 ignored", r.status_code == 200 and r.json().get("user_state") == "ignored")

    print("\n" + ("冒烟全部通过 ✅" if ok else "存在失败项 ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
