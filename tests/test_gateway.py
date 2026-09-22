"""网关单元测试：策略映射、Mock 确定性、成本埋点、供应商回退与结构化输出修复。
全程不触发真实模型。
"""
from __future__ import annotations

import pytest
from fastapi import Depends
from pydantic import BaseModel

from app.core import trace
from app.core.config import settings
from app.domain.models.cost_log import CostLog
from app.llm.completion import Completion
from app.llm.exceptions import NonRetryableLLMError, RetryableLLMError
from app.llm.gateway import ModelGateway, strategy_for, Strategy
from app.llm.provider import LLMProvider


def test_strategy_mapping():
    assert strategy_for("deep_reasoning").reasoning is True
    assert strategy_for("batch_extraction").reasoning is False
    # 标准对话关思考（与设计文档 §3.1.2 一致；深度任务才开）
    assert strategy_for("multi_turn_dialogue").reasoning is False
    assert strategy_for("l1_mining").reasoning is True
    assert strategy_for("unknown_task") == strategy_for("default")
    assert strategy_for("unknown_task").reasoning is False


def test_gateway_routes_task_to_reasoning():
    gw = ModelGateway()  # 无 KEY → MockProvider
    strat = gw.route("conflict_detection")
    assert isinstance(strat, Strategy)
    assert strat.reasoning is True
    assert strat.model  # 回落 settings.active_model（按当前供应商取），非空


def test_gateway_chat_deterministic_and_billable(session):
    gw = ModelGateway()
    with trace.request_id("req-test-gateway"):
        c1 = gw.chat(task_type="multi_turn_dialogue", messages=[{"role": "user", "content": "你好"}], session=session)
        c2 = gw.chat(task_type="multi_turn_dialogue", messages=[{"role": "user", "content": "你好"}], session=session)
    # MockProvider 确定性：相同输入 → 相同输出与 token
    assert c1.text == c2.text
    assert c1.reasoning is False
    assert c1.prompt_tokens > 0

    # 成本落库（以本测试 request_id 过滤，避免跨测试共享内存库污染）
    session.flush()
    rows = session.query(CostLog).filter(CostLog.request_id == "req-test-gateway").all()
    assert len(rows) == 2
    assert all(r.reasoning is False for r in rows)
    assert all(r.estimated_cost >= 0 for r in rows)


def test_prompt_version_recorded_in_cost_log(session):
    """提示词版本落库：默认走策略表反查，显式传参覆盖（一个 task_type 多提示词场景）。"""
    from app.llm.prompts import PROMPT_VERSION_BY_TASK_TYPE

    gw = ModelGateway()
    with trace.request_id("req-prompt-ver"):
        # 策略表默认版本（l1_mining → 对应 L1_ROUTE 版本）
        gw.chat(task_type="l1_mining", messages=[{"role": "user", "content": "x"}], session=session)
        # 显式覆盖（cognitive_brief 默认 l3_brief，此处模拟衔接追问传 l3_item 版本）
        gw.chat(
            task_type="cognitive_brief",
            messages=[{"role": "user", "content": "x"}],
            session=session,
            prompt_version="v2-custom",
        )
    session.flush()
    rows = session.query(CostLog).filter(CostLog.request_id == "req-prompt-ver").order_by(CostLog.id).all()
    assert len(rows) == 2
    assert rows[0].prompt_version == PROMPT_VERSION_BY_TASK_TYPE["l1_mining"]
    assert rows[1].prompt_version == "v2-custom"


# ---------- 供应商回退（LLM-4 运行时回退）----------


class _ScriptedProvider(LLMProvider):
    """按脚本回放文本的供应商：记录每次调用入参，便于断言重试 / 回退 / 约束注入。"""

    supports_json_object = False
    supports_json_schema = False

    def __init__(self, texts: list[str], *, model_name: str = "", error: Exception | None = None):
        self._texts = list(texts)
        self.default_model = model_name  # 实例属性；网关按 getattr(provider, "default_model") 取
        self.calls: list[dict] = []
        self._error = error
        # 给了 error 就默认「每次都失败」（重试与回退都要触发）
        self._fail_forever = error is not None

    def chat(self, *, model, reasoning, messages, tools=None, response_format=None, task_type="default"):
        self.calls.append({
            "model": model, "task_type": task_type,
            "messages": messages, "response_format": response_format,
        })
        if self._fail_forever:
            raise self._error
        text = self._texts.pop(0) if self._texts else ""
        return Completion(text=text, prompt_tokens=1, completion_tokens=1,
                          finish_reason="stop", model=model, reasoning=reasoning)


class _JsonCapable(_ScriptedProvider):
    """声明支持原生 JSON 约束的供应商（对应真实的 MiMoProvider）。"""

    supports_json_object = True
    supports_json_schema = True


class _Probe(BaseModel):
    topic: str


def test_provider_chain_follows_settings(monkeypatch):
    """回退链由 KEY 决定：两个都配 → 主 + 备；关掉开关 → 只留主；无 KEY → Mock。"""
    from app.llm.deepseek.mock import MockProvider
    from app.llm.deepseek.provider import DeepSeekProvider
    from app.llm.gateway import _build_provider_chain
    from app.llm.mimo.provider import MiMoProvider  # noqa: F401 仅确保可导入

    monkeypatch.setattr(settings, "mimo_api_key", "sk-mimo-test")
    monkeypatch.setattr(settings, "deepseek_api_key", "sk-ds-test")
    monkeypatch.setattr(settings, "llm_fallback_enabled", True)
    assert [type(p).__name__ for p in _build_provider_chain()] == ["MiMoProvider", "DeepSeekProvider"]

    monkeypatch.setattr(settings, "llm_fallback_enabled", False)
    assert [type(p).__name__ for p in _build_provider_chain()] == ["MiMoProvider"]

    monkeypatch.setattr(settings, "mimo_api_key", "")
    assert [type(p).__name__ for p in _build_provider_chain()] == ["DeepSeekProvider"]

    monkeypatch.setattr(settings, "deepseek_api_key", "")
    assert isinstance(_build_provider_chain()[0], MockProvider)


def test_fallback_switches_to_secondary_on_unretryable_error():
    """主供应商 400（如模型名错）→ 立刻切备用，且用**备用自己的**模型名发请求。"""
    dead = _ScriptedProvider([], model_name="mimo-x", error=NonRetryableLLMError("400 Unsupported model"))
    alive = _ScriptedProvider(["备用供应商的回复"], model_name="deepseek-fallback")
    gw = ModelGateway(providers=[dead, alive])

    c = gw.chat(task_type="multi_turn_dialogue", messages=[{"role": "user", "content": "hi"}])

    assert c.text == "备用供应商的回复"
    assert len(dead.calls) == 1  # 不可重试错误不浪费重试
    assert alive.calls[0]["model"] == "deepseek-fallback"


def test_fallback_after_retries_exhausted(monkeypatch):
    """可重试错误（超时/限流）先按重试策略试满 3 次，仍失败才切备用。"""
    monkeypatch.setattr("app.llm.retry.time.sleep", lambda *_a, **_kw: None)
    flaky = _ScriptedProvider([], model_name="mimo-x", error=RetryableLLMError("连接超时"))
    alive = _ScriptedProvider(["备用回复"], model_name="deepseek-fallback")
    gw = ModelGateway(providers=[flaky, alive])

    c = gw.chat(task_type="multi_turn_dialogue", messages=[{"role": "user", "content": "hi"}])

    assert c.text == "备用回复"
    assert len(flaky.calls) == 3  # 主供应商重试 3 次


def test_fallback_not_triggered_when_primary_succeeds():
    alive = _ScriptedProvider(["主供应商的回复"], model_name="mimo-x")
    standby = _ScriptedProvider(["不该被用到"], model_name="deepseek-fallback")
    gw = ModelGateway(providers=[alive, standby])

    c = gw.chat(task_type="multi_turn_dialogue", messages=[{"role": "user", "content": "hi"}])

    assert c.text == "主供应商的回复"
    assert standby.calls == []


def test_no_fallback_when_chain_has_single_provider():
    """链上只有一个供应商时失败要如实上抛——不能悄悄用别的什么东西兜住。"""
    dead = _ScriptedProvider([], model_name="mimo-x", error=NonRetryableLLMError("401 鉴权失败"))
    gw = ModelGateway(providers=[dead])

    with pytest.raises(NonRetryableLLMError):
        gw.chat(task_type="multi_turn_dialogue", messages=[{"role": "user", "content": "hi"}])


def test_fallback_recomputes_json_constraint_for_secondary():
    """切到不支持原生 JSON 的供应商时，response_format 必须撤掉（否则请求直接被拒）。"""
    dead = _JsonCapable([], model_name="mimo-x", error=NonRetryableLLMError("400"))
    plain = _ScriptedProvider(['{"topic":"ok"}'], model_name="deepseek-fallback")
    gw = ModelGateway(providers=[dead, plain])

    gw.chat(task_type="topic_analysis", messages=[{"role": "user", "content": "x"}], json_model=_Probe)

    assert plain.calls[0]["response_format"] is None


def test_fallback_adds_json_constraint_for_capable_secondary():
    """反向：主供应商不支持约束、备用支持 → 备用这一跳要带上 json_schema。"""
    dead = _ScriptedProvider([], model_name="deepseek-x", error=NonRetryableLLMError("500"))
    capable = _JsonCapable(['{"topic":"ok"}'], model_name="mimo-fallback")
    gw = ModelGateway(providers=[dead, capable])

    gw.chat(task_type="topic_analysis", messages=[{"role": "user", "content": "x"}], json_model=_Probe)

    fmt = capable.calls[0]["response_format"]
    assert fmt["type"] == "json_schema" and "topic" in str(fmt["json_schema"])


# ---------- 结构化输出修复回合 ----------


def test_json_repair_round_recovers(monkeypatch, session):
    monkeypatch.setattr(settings, "llm_json_repair_enabled", True)
    p = _ScriptedProvider(["这不是 JSON", '{"topic":"修好了"}'])
    gw = ModelGateway(provider=p)

    with trace.request_id("req-json-repair"):
        c = gw.chat(
            task_type="topic_analysis", messages=[{"role": "user", "content": "x"}],
            json_model=_Probe, session=session, user_id="u1",
        )

    assert c.text == '{"topic":"修好了"}'
    assert len(p.calls) == 2
    repair = p.calls[1]["messages"]
    assert repair[-2]["role"] == "assistant" and repair[-2]["content"] == "这不是 JSON"
    assert repair[-1]["role"] == "user" and "JSON" in repair[-1]["content"]

    # 修复回合是真实调用，token 必须一并记账（否则这部分开销不可见）
    session.flush()
    rows = session.query(CostLog).filter(CostLog.request_id == "req-json-repair").all()
    assert len(rows) == 2


def test_json_repair_gives_up_and_returns_original(monkeypatch):
    """修复仍失败 → 原样返回原始输出，降级仍由调用方既有的路径处理，网关不改语义。"""
    monkeypatch.setattr(settings, "llm_json_repair_enabled", True)
    p = _ScriptedProvider(["破的", "还是破的"])
    gw = ModelGateway(provider=p)

    c = gw.chat(task_type="topic_analysis", messages=[{"role": "user", "content": "x"}], json_model=_Probe)

    assert c.text == "破的"
    assert len(p.calls) == 2  # 只修一轮，不无限重试


def test_json_repair_skipped_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "llm_json_repair_enabled", False)
    p = _ScriptedProvider(["破的"])
    gw = ModelGateway(provider=p)

    c = gw.chat(task_type="topic_analysis", messages=[{"role": "user", "content": "x"}], json_model=_Probe)

    assert c.text == "破的"
    assert len(p.calls) == 1


def test_free_text_task_never_triggers_repair(monkeypatch):
    """没有 json_model 的调用（闲聊等）不校验也不修复——非 JSON 文本在这里是合法结果。"""
    monkeypatch.setattr(settings, "llm_json_repair_enabled", True)
    p = _ScriptedProvider(["这不是 JSON，只是句人话"])
    gw = ModelGateway(provider=p)

    c = gw.chat(task_type="multi_turn_dialogue", messages=[{"role": "user", "content": "hi"}])

    assert len(p.calls) == 1
    assert c.text.startswith("这不是 JSON")


def test_repair_round_keeps_json_constraint(monkeypatch):
    """修复回合同样带约束——否则等于「再用自由文本赌一次」，成功率不会更高。"""
    monkeypatch.setattr(settings, "llm_json_repair_enabled", True)
    p = _JsonCapable(["破的", '{"topic":"ok"}'])
    gw = ModelGateway(provider=p)

    gw.chat(task_type="topic_analysis", messages=[{"role": "user", "content": "x"}], json_model=_Probe)

    assert p.calls[1]["response_format"]["type"] == "json_schema"


def test_repair_round_failure_does_not_break_call(monkeypatch):
    """修复回合本身报错（网络断了）时不能把整次调用带崩——沿用原始输出。"""
    monkeypatch.setattr(settings, "llm_json_repair_enabled", True)
    monkeypatch.setattr("app.llm.retry.time.sleep", lambda *_a, **_kw: None)

    class _BrokenOnRepair(_ScriptedProvider):
        def chat(self, *, model, reasoning, messages, tools=None, response_format=None, task_type="default"):
            if self.calls:  # 第 2 次调用（修复回合）直接炸
                self.calls.append({"model": model, "task_type": task_type, "messages": messages,
                                   "response_format": response_format})
                raise RetryableLLMError("修复回合网络断了")
            self.calls.append({"model": model, "task_type": task_type, "messages": messages,
                               "response_format": response_format})
            return Completion(text="破的", prompt_tokens=1, completion_tokens=1, model=model,
                              reasoning=reasoning)

    p = _BrokenOnRepair([])
    gw = ModelGateway(provider=p)

    c = gw.chat(task_type="topic_analysis", messages=[{"role": "user", "content": "x"}], json_model=_Probe)

    assert c.text == "破的"