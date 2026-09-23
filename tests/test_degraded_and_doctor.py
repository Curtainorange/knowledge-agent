"""无 Key 降级档（L1 规则路由）与 doctor 自检端点测试。"""
from __future__ import annotations

from app.agent.l1_orchestrator import L1Orchestrator
from app.ingestion.service import IngestionService
from app.llm.gateway import ModelGateway
from app.retrieval.embedding import HashEmbedding
from app.retrieval.retriever import Retriever
from app.retrieval.vector_store import InMemoryVectorStore
from tests.helpers import sign_in


def _seed(session, title: str, content: str):
    return IngestionService(session, HashEmbedding()).add_knowledge(
        user_id="u1", title=title, content=content
    )


def _store_with_vectors(session, items):
    store = InMemoryVectorStore()
    emb = HashEmbedding()
    for item in items:
        vec = emb.embed([item.title + "\n" + item.raw_content])[0]
        store.upsert(item_id=item.id, user_id="u1", vector=vec, title=item.title, content=item.raw_content)
    return store


# ---- L1 规则降级档 ----------------------------------------------------------


def _rule_orchestrator(session, store) -> L1Orchestrator:
    # conftest 已清空两个 KEY：ModelGateway() 默认构造 → 链上只有 MockProvider
    return L1Orchestrator(
        ModelGateway(), session,
        retriever=Retriever(HashEmbedding(), store),
    )


class _RealLikeProvider:
    """带 is_mock=False 的假件：不需要真实 Key（DeepSeekProvider 会校验凭证）。"""

    is_mock = False


def test_gateway_has_real_provider_reflects_mock_only_chain():
    assert ModelGateway().has_real_provider is False  # 测试环境无 Key → Mock-only
    assert ModelGateway(provider=_RealLikeProvider()).has_real_provider is True


def test_rule_route_locates_on_double_channel_hit(session):
    item = _seed(session, "数据库索引", "B+树与哈希索引的区别")
    orch = _rule_orchestrator(session, _store_with_vectors(session, [item]))
    result = orch.mine(user_id="u1", conversation_id=None, message="数据库索引")
    assert result.state == "located"
    assert [i.item_id for i in result.located_items] == [item.id]
    assert "无 Key 降级" in result.reason


def test_rule_route_clarifies_when_signal_weak(session):
    _seed(session, "数据库索引", "B+树与哈希索引的区别")
    orch = _rule_orchestrator(session, _store_with_vectors(session, []))
    # 清空 store：两路都无信号 → 只能追问
    result = orch.mine(user_id="u1", conversation_id=None, message="番茄炒蛋怎么做")
    assert result.state == "clarifying"
    assert result.question


def test_rule_route_never_calls_mock(session, monkeypatch):
    """降级档的铁律：Mock 输出不能被当模型结论——规则路径必须绕开 gateway.chat。"""
    item = _seed(session, "数据库索引", "B+树与哈希索引的区别")
    orch = _rule_orchestrator(session, _store_with_vectors(session, [item]))

    def boom(self, *args, **kwargs):
        raise AssertionError("规则降级档不应发起模型调用")

    monkeypatch.setattr(ModelGateway, "chat", boom)
    result = orch.mine(user_id="u1", conversation_id=None, message="数据库索引")
    assert result.state == "located"


def test_real_provider_path_unaffected_by_rule_flag(session, monkeypatch):
    """配了真实 Key（测试注入假 provider）时走原模型路由，规则档不介入。"""
    from app.llm.gateway import ModelGateway as _GW

    item = _seed(session, "数据库索引", "B+树与哈希索引的区别")

    class FakeProvider:
        is_mock = False

        def chat(self, **kwargs):
            class C:
                text = '{"decision": "located", "item_ids": ["%s"], "question": "", "reason": "test"}' % item.id
                prompt_tokens = 1
                completion_tokens = 1
                cached_tokens = 0
                model = "fake"
                reasoning = False
                finish_reason = "stop"

            return C()

    orch = L1Orchestrator(
        _GW(provider=FakeProvider()), session,
        retriever=Retriever(HashEmbedding(), _store_with_vectors(session, [item])),
    )
    result = orch.mine(user_id="u1", conversation_id=None, message="数据库索引")
    assert result.state == "located"
    assert result.reason == "test"


# ---- doctor 自检端点 --------------------------------------------------------


def test_doctor_requires_auth(client):
    assert client.get("/api/v1/system/doctor").status_code == 401


def test_doctor_reports_degraded_without_keys(client):
    user = sign_in(client, "doctor_user")
    resp = client.get("/api/v1/system/doctor", headers=user.headers)
    assert resp.status_code == 200
    body = resp.json()
    # 测试环境：数据库 / 嵌入 / 向量索引都可用；无 Key → LLM 项 degraded
    assert body["status"] == "degraded"
    assert body["checks"]["database"]["ok"] is True
    assert body["checks"]["embedding"]["ok"] is True
    assert body["checks"]["llm"]["ok"] is False
    assert "未配置模型 Key" in body["checks"]["llm"]["detail"]
