"""L3 真实链路冒烟：构造「全是入门、缺进阶」的知识库，看认知助产能否指出缺口。

用法（项目根目录，KEY 从 .env 读取）：
    python scripts/smoke_l3.py

为什么用这个场景：设计文档给 L3 的原始例子就是「知识库里全是『如何开始健身』
『如何开始写作』——全是入门级」，助产士应当指出「瓶颈不在如何开始，而在平台期」。
因此这里刻意灌 6 条入门内容，看模型是否真的能从统计里读出这个失衡，
而不是给一段放之四海皆可的鸡汤。
"""
from __future__ import annotations

import os
import sys
import uuid

os.environ.setdefault("DATABASE_URL", "sqlite:///./smoke_l3_tmp.db")
try:  # 每次冒烟全新状态
    os.remove("smoke_l3_tmp.db")
except FileNotFoundError:
    pass

from fastapi.testclient import TestClient  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.main import app  # noqa: E402

# 刻意全是入门级内容：真正的缺口在「入门之后」
ITEMS = [
    ("如何开始健身", "第一步是选择适合自己的运动，从每周三次、每次二十分钟开始，先建立习惯。"),
    ("如何开始跑步", "新手跑步从走跑结合开始，注意选一双合脚的跑鞋，避免一开始就追求配速。"),
    ("如何开始写作", "第一步是每天写一百字，不追求质量，先把写的动作固定下来。"),
    ("如何开始学英语", "从每天背二十个单词开始，配合简单的听力材料，先让输入变得轻松。"),
    ("如何开始读书", "先选一本薄一点、自己感兴趣的书，每天读十页，读完再换下一本。"),
    ("如何开始冥想", "每天五分钟，专注呼吸，走神了就轻轻把注意力带回来，不要评判自己。"),
]


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
          f"当前={settings.model_provider} / {settings.active_model}")

    username = f"smoke_l3_{uuid.uuid4().hex[:8]}"
    r = client.post("/api/v1/auth/register", json={"username": username, "password": "smoke12345"})
    check("注册", r.status_code == 201)
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

    for title, content in ITEMS:
        resp = client.post(
            "/api/v1/knowledge/items", json={"title": title, "content": content}, headers=headers
        )
        check(f"录入「{title}」", resp.status_code == 200)

    print(f"\n→ 触发 L3 认知简报（真实模型 {settings.active_model}：归类 + 生成追问，请稍候）...")
    r = client.post("/api/v1/l3/brief", headers=headers)
    check("POST /l3/brief", r.status_code == 200, r.text[:300] if r.status_code != 200 else "")
    body = r.json()
    print(f"   state={body['state']} 分析条目={body['analyzed_items']} 主题数={len(body['topics'])}")
    if body["note"]:
        print(f"   note: {body['note']}")

    print("\n   主题分布：")
    for topic in body["topics"]:
        levels = "、".join(f"{k} {v}" for k, v in topic["levels"].items())
        print(f"     - {topic['topic']}：{topic['count']} 篇（{levels}）")

    print("\n   结构信号：")
    for pattern in body["patterns"]:
        print(f"     · {pattern}")

    print("\n   生成的问题：")
    for i, q in enumerate(body["questions"], 1):
        print(f"     {i}. {q['question']}")
        if q.get("why"):
            print(f"        为什么问：{q['why']}")
        if q.get("evidence"):
            print(f"        数据依据：{q['evidence']}")
        if q.get("next_step"):
            print(f"        可以做什么：{q['next_step']}")
        print()

    check("产出结构信号", bool(body["patterns"]))
    check("产出追问", len(body["questions"]) >= 1)
    check("每条追问都带数据依据", all(q.get("evidence") for q in body["questions"]))
    check("主题分布非空", bool(body["topics"]))

    # 单条衔接追问（UC-L3-02）
    items = client.get("/api/v1/knowledge/items", headers=headers).json()["items"]
    r = client.post("/api/v1/l3/question", json={"item_id": items[0]["item_id"]}, headers=headers)
    check("POST /l3/question", r.status_code == 200)
    qs = r.json()["questions"]
    if qs:
        print(f"\n   针对「{items[0]['title']}」的衔接追问：{qs[0]['question']}")
        print(f"        数据依据：{qs[0].get('evidence', '')}")
    check("衔接追问非空", bool(qs))

    print("\n" + ("冒烟全部通过 ✅" if ok else "存在失败项 ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())