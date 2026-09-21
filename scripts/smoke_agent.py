"""对话工作台人工验收脚本：同步能力 / 异步回合 / 卡片内操作回流。

跑法：python scripts/smoke_agent.py

全程不触网、不碰真实库（MockProvider + hash embedding + 临时 SQLite）。
看四件事——每句话落到哪个能力、回哪种卡片、后台任务是否把结果写回消息、
以及卡片上的操作是否真的落库。

已知局限：worker 执行时会自建 `ModelGateway()`，所以走后台路径的能力
拿不到本脚本的注入（与既有 l2_scan 处理器同一形态）。因此第 2 段验证的是
**异步管道**（pending → 结果卡），第 3 段直接构造一条冲突来验证**操作回流**。
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402

_db = os.path.join(tempfile.mkdtemp(prefix="cc_smoke_agent_"), "smoke.db")
settings.deepseek_api_key = ""
settings.embedding_backend = "hash"
settings.weread_api_key = ""
settings.books_dir = tempfile.mkdtemp()
settings.worker_enabled = True          # 开着才会走异步路径
settings.jwt_secret = "smoke-secret"
settings.password_pbkdf2_iterations = 1000
settings.task_retry_backoff_seconds = 0.0
settings.database_url = "sqlite:///" + _db

from fastapi.testclient import TestClient  # noqa: E402

from app.agent import turns  # noqa: E402
from app.domain.db import SessionLocal  # noqa: E402
from app.domain.repositories.conflict_repository import ConflictRepository  # noqa: E402
from app.domain.repositories.conversation_repository import ConversationRepository  # noqa: E402
from app.domain.repositories.knowledge_repository import KnowledgeRepository  # noqa: E402
from app.main import app  # noqa: E402
from app.workers import runner  # noqa: E402

PASSWORD = "pw12345678"
client = TestClient(app)
client.post("/api/v1/auth/register",
            json={"username": "smoke_agent", "password": PASSWORD, "nickname": "smoke"})
token = client.post("/api/v1/auth/login",
                    json={"username": "smoke_agent", "password": PASSWORD}).json()["access_token"]
headers = {"Authorization": f"Bearer {token}"}
me = client.get("/api/v1/auth/me", headers=headers).json()
USER_ID = me["user_id"]


def chat(message, conversation_id=None):
    resp = client.post("/api/v1/agent/chat",
                       json={"conversation_id": conversation_id, "message": message},
                       headers=headers)
    body = resp.json()
    card = body.get("card") or {}
    print(f"--- {message}    [HTTP {resp.status_code}]")
    print(f"    能力={body.get('capability')}  判定={body.get('decided_by')}  "
          f"卡片={card.get('kind') or '-'}")
    print(f"    回复={str(body.get('reply'))[:72]}")
    return body


print("=== 1. 同步能力（秒级，直接出结果）===\n")
first = chat("记一下：B+树更适合范围查询 #数据库")
cid = first["conversation_id"]
for text in ("之前存的那段讲查询优化的内容", "生成认知简报", "今天天气不错", "我的书架里有什么"):
    chat(text, cid)

print("\n=== 2. 异步回合（L2 扫描：先占位，后台跑完写回）===\n")
pending = chat("扫描知识冲突", cid)
turn_id = (pending.get("card") or {}).get("turn_id")
print(f"    → 已入队 turn={str(turn_id)[:8]}")

with SessionLocal() as session:
    summary = runner.run_once(session)
print(f"    → worker 跑完一轮：claimed={summary.claimed} ok={summary.succeeded} "
      f"retry={summary.retried} dead={summary.dead}")

conv = client.get(f"/api/v1/agent/conversation/{cid}", headers=headers).json()
last = conv["messages"][-1]
print(f"    → 读回会话：卡片={last['card'].get('kind')}  key={str(last['card'].get('key'))[:8]}")
print(f"    → 消息正文已被就地改写：{last['content'][:72]}")
if last["card"].get("summary"):
    print(f"    → 扫描统计：{last['card']['summary']}")

print("\n=== 3. 卡片内操作回流（构造一条冲突，验证采纳/忽略真的落库）===\n")
with SessionLocal() as session:
    krepo = KnowledgeRepository(session, user_id=USER_ID)
    item_a = krepo.create(user_id=USER_ID, title="专注优先",
                          content="多任务并行是效率的关键，应该同时推进多个项目。")
    item_b = krepo.create(user_id=USER_ID, title="专注至上",
                          content="多任务并行必然降低效率，必须一次只做一件事。")
    session.commit()
    conflict = ConflictRepository(session, user_id=USER_ID).create(
        user_id=USER_ID, item_a_id=item_a.id, item_b_id=item_b.id,
        claim_a_id="claim-a", claim_b_id="claim-b",
        conflict_type="立场对立", detail="一边要多任务，一边说必须单任务",
        suggestion="挑一个真实场景实测再定", confidence=0.82,
    )
    session.commit()

    conversation = ConversationRepository(session, user_id=USER_ID).create(user_id=USER_ID)
    card_key = "smoke-card-1"
    card = turns.l2_conflicts_card(
        key=card_key,
        items=turns.conflict_views(session, user_id=USER_ID, conflict_ids=[conflict.id]),
        summary={"scanned_items": 2, "pairs_judged": 1, "conflicts_found": 1},
    )
    repo = ConversationRepository(session, user_id=USER_ID)
    repo.append_message(conversation, "user", "扫描知识冲突", source=turns.SOURCE)
    repo.append_message(conversation, "assistant", "发现 1 处矛盾", source=turns.SOURCE, card=card)
    session.commit()
    demo_cid, conflict_id = conversation.id, conflict.id

print(f"    冲突卡：{card['items'][0]['title_a']} ↔ {card['items'][0]['title_b']}"
      f"（当前状态 {card['items'][0]['user_state']}）")

action = client.post("/api/v1/agent/actions", headers=headers, json={
    "conversation_id": demo_cid, "card_key": card_key,
    "action": "l2.conflict.state", "target_id": conflict_id, "value": "ignored",
}).json()
print(f"    → 点「忽略」：{action['reply']}")
print(f"    → 返回的卡片状态：{[i['user_state'] for i in action['card']['items']]}")

with SessionLocal() as session:
    stored = ConflictRepository(session, user_id=USER_ID).get(conflict_id)
    print(f"    → 数据库里的状态：{stored.user_state}（真的落库了，不是只改了回包）")

print("\n=== 4. 能力目录（wired=是否已接进对话）===\n")
for item in client.get("/api/v1/agent/capabilities").json():
    flag = "已接入" if item["wired"] else "引导卡"
    print(f"    {item['capability']:<15} {item['label']:<8} {flag:<6}  {item['example']}")
