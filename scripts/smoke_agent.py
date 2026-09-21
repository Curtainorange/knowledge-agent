"""对话入口（app.html）人工验收脚本：打印一次完整会话的分流与卡片。

跑法：python scripts/smoke_agent.py

不触网、不碰真实库：MockProvider + hash embedding + 临时 SQLite。
看三件事——每句话落到哪个能力上、回的是哪种卡片、会话里实际写下了什么。
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402

_db_path = os.path.join(tempfile.mkdtemp(prefix="cc_smoke_agent_"), "smoke.db")
settings.deepseek_api_key = ""
settings.embedding_backend = "hash"
settings.weread_api_key = ""
settings.books_dir = tempfile.mkdtemp()
settings.worker_enabled = False
settings.jwt_secret = "smoke-secret"
settings.password_pbkdf2_iterations = 1000
settings.database_url = "sqlite:///" + _db_path

from fastapi.testclient import TestClient  # noqa: E402

from app.domain.db import SessionLocal  # noqa: E402
from app.domain.models.conversation import Conversation  # noqa: E402
from app.main import app  # noqa: E402

PASSWORD = "pw12345678"
client = TestClient(app)
client.post(
    "/api/v1/auth/register",
    json={"username": "smoke_agent", "password": PASSWORD, "nickname": "smoke"},
)
token = client.post(
    "/api/v1/auth/login", json={"username": "smoke_agent", "password": PASSWORD}
).json()["access_token"]
headers = {"Authorization": f"Bearer {token}"}


def send(message: str, conversation_id: str | None = None) -> dict:
    resp = client.post(
        "/api/v1/agent/chat",
        json={"conversation_id": conversation_id, "message": message},
        headers=headers,
    )
    body = resp.json()
    card = body.get("card") or {}
    print(f"--- {message}    [HTTP {resp.status_code}]")
    print(f"    能力={body.get('capability')}  判定={body.get('decided_by')}  卡片={card.get('kind') or '-'}")
    print(f"    回复={str(body.get('reply'))[:70]}")
    return body


print("=== 对话入口验收 ===\n")
first = send("记一下：B+树更适合范围查询 #数据库")
cid = first["conversation_id"]
for text in ("之前存的那段讲查询优化的内容", "扫描知识冲突", "今天天气不错", "同步微信读书"):
    send(text, cid)

print("\n=== 能力目录（wired=是否已接进对话） ===")
for item in client.get("/api/v1/agent/capabilities").json():
    print(f"  {item['capability']:<15} {item['label']:<8} wired={item['wired']}  {item['example']}")

print("\n=== 会话里实际写下的消息 ===")
with SessionLocal() as session:
    conversation = session.get(Conversation, cid)
    print(f"  共 {len(conversation.messages)} 条，state={conversation.state}")
    for message in conversation.messages:
        card_kind = (message.get("card") or {}).get("kind", "-")
        print(f"  {message['role']:<9} source={message.get('source', '-'):<5} card={card_kind:<18} {message['content'][:40]}")
