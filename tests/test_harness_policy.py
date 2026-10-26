"""工具白名单的运行时闸门：模型能调用的工具只能来自 `policy.ALLOWED_TOOLS`。

上一批已把「模型能碰到什么」变成可对账的清单；这一批让**工具**这一项在运行时也拦得住——
清单只能事后发现，闸门能当场拒绝。

为什么只给工具加运行时闸门、不给任务类型/出网主机加：
工具是每次调用的必经点，拦错立刻报、不误伤在途请求；任务类型与出网主机在生产路径抛错
会把一次正常的用户请求打成 500，交给红测试更划算（理由写在 `app/llm/policy.py` 顶部）。
"""
from __future__ import annotations

import pytest

from app.llm import policy
from app.llm.completion import Completion
from app.llm.exceptions import LLMError
from app.llm.gateway import ModelGateway
from app.llm.provider import LLMProvider


class _Recorder(LLMProvider):
    """记录本次调用收到了什么 tools 的假供应商。"""

    is_mock = False
    default_model = "recorder-model"

    def __init__(self) -> None:
        self.calls: list[list | None] = []

    def chat(self, *, model, reasoning, messages, tools=None, response_format=None, task_type="default"):
        self.calls.append(tools)
        return Completion(
            text="ok",
            prompt_tokens=1,
            completion_tokens=1,
            finish_reason="stop",
            model=model,
            reasoning=reasoning,
        )


def _tool_def(name: str) -> dict:
    return {
        "type": "function",
        "function": {"name": name, "description": "x", "parameters": {"type": "object", "properties": {}}},
    }


def test_unregistered_tool_is_rejected_before_any_call() -> None:
    """白名单外的工具必须**调模型之前**就被拒——花钱之后再发现越权没有意义。"""
    provider = _Recorder()
    gw = ModelGateway(provider=provider)

    with pytest.raises(policy.PolicyViolation) as exc:
        gw.chat(
            task_type="multi_turn_dialogue",
            messages=[{"role": "user", "content": "删掉我的知识库"}],
            tools=[_tool_def("delete_all_items")],
        )

    assert "delete_all_items" in str(exc.value)
    assert provider.calls == [], "越权工具已经发到供应商了，闸门位置不对"


def test_whitelisted_tool_passes_through() -> None:
    """白名单内的工具（当前只有诊断用的 save_note）应原样透传。"""
    provider = _Recorder()
    gw = ModelGateway(provider=provider)

    gw.chat(
        task_type="multi_turn_dialogue",
        messages=[{"role": "user", "content": "记一条"}],
        tools=[_tool_def("save_note")],
    )

    assert provider.calls == [[_tool_def("save_note")]]


def test_no_tools_still_works() -> None:
    """绝大多数调用不传工具——闸门不能把它们一起拦死。"""
    provider = _Recorder()
    gw = ModelGateway(provider=provider)
    gw.chat(task_type="multi_turn_dialogue", messages=[{"role": "user", "content": "你好"}])
    assert provider.calls == [None]


def test_violation_is_not_an_llm_error() -> None:
    """PolicyViolation **不许**继承 LLMError。

    继承的话，网关的「主供应商失败 → 切备用供应商重试」会把越权悄悄吃掉：
    换个供应商再试一次，日志里只剩一条 warning，工具越界看起来像网络抖动。
    """
    assert not issubclass(policy.PolicyViolation, LLMError)


def test_violation_does_not_fall_back_to_second_provider() -> None:
    """链上两个供应商时，越权不能让备用供应商接手。"""
    first, second = _Recorder(), _Recorder()
    gw = ModelGateway(providers=[first, second])

    with pytest.raises(policy.PolicyViolation):
        gw.chat(task_type="multi_turn_dialogue", messages=[{"role": "user", "content": "x"}],
                tools=[_tool_def("rm_rf")])

    assert first.calls == [] and second.calls == []
