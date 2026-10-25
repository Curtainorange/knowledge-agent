"""多轮指代：分流要看得见「刚才在说什么」。

没有历史时，「再来一次」「那个呢」必然落到通用对话——分流只看当前句，
根本不知道上一轮在干什么。这里钉四件事：

- 历史进了模型分流的 prompt，且与当前句**同一条** user 消息（分两条会被当成上一轮输入）；
- 命令式本地规则依然优先，不受历史影响（「记一下…」不该因为上一轮聊过别的被改写）；
- 预算从最近往回算：超了丢旧的，最近那句必须在；
- 没有历史时 prompt 与改动前一致（向后兼容）。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.agent.copilot import Copilot
from app.agent.router import (
    HISTORY_ITEM_CHARS,
    CapabilityRouter,
    _history_block,
    _match_local,
)
from app.llm.completion import Completion
from app.llm.gateway import ModelGateway
from app.llm.provider import LLMProvider


class RecordingProvider(LLMProvider):
    """记录最后一次 messages 的分流供应商。"""

    def __init__(self, row: dict | None = None) -> None:
        self.row = row or {"capability": "suggestion", "confidence": 0.9, "reason": "沿用上一轮"}
        self.messages: list[dict] = []

    def chat(
        self, *, model, reasoning, messages, tools=None, response_format=None, task_type="default"
    ) -> Completion:
        self.messages = list(messages)
        import json

        return Completion(
            text=json.dumps(self.row, ensure_ascii=False),
            prompt_tokens=1, completion_tokens=1, finish_reason="stop",
            model=model, reasoning=reasoning,
        )


def _router(session, row: dict | None = None):
    provider = RecordingProvider(row)
    return CapabilityRouter(ModelGateway(provider=provider), session), provider


def _user_content(provider: RecordingProvider) -> str:
    """只看 user 消息：system 提示词里也有「最近几轮对话」这个说法，
    拼进来会让「没有历史」的断言永远失败（踩过一次）。"""
    return "\n".join(
        str(m.get("content") or "") for m in provider.messages if m.get("role") == "user"
    )


# ---------- 历史进 prompt ----------


def test_history_is_included_with_current_message(session):
    router, provider = _router(session)
    history = [
        {"role": "user", "content": "怎么把极度透明用到周会上"},
        {"role": "assistant", "content": "先说清楚你要解决什么"},
    ]
    route = router.route("再来一次", user_id="u1", history=history)

    content = _user_content(provider)
    assert "怎么把极度透明用到周会上" in content
    assert "先说清楚你要解决什么" in content
    # 当前句与历史必须在同一条 user 消息里：分两条时模型会先答历史那句
    assert "再来一次" in content
    user_msgs = [m for m in provider.messages if m["role"] == "user"]
    assert len(user_msgs) == 1
    assert route.capability == "suggestion"


def test_no_history_keeps_prompt_shape(session):
    """没有历史时 prompt 与改动前一致（直接测 prompt 组装，绕开本地规则）。"""
    router, provider = _router(session)
    router._model_route("帮我看看这个概念是什么意思", user_id="u1")

    content = _user_content(provider)
    assert "最近几轮对话" not in content
    assert "用户这句话是：" in content


def test_empty_history_behaves_like_none(session):
    assert _history_block(None) == ""
    assert _history_block([]) == ""
    assert _history_block([{"role": "user", "content": "   "}]) == ""


# ---------- 预算 ----------


def test_only_last_two_turns_are_sent():
    """条数就是预算：6 条历史只取最近 4 条（两轮）。"""
    history = []
    for i in range(6):
        history.append({"role": "user" if i % 2 == 0 else "assistant", "content": f"第{i}句"})
    history.append({"role": "user", "content": "刚才在排周计划"})

    block = _history_block(history)
    assert "刚才在排周计划" in block
    assert "第0句" not in block
    assert "第5句" in block


def test_long_item_is_clipped_with_ellipsis():
    block = _history_block([{"role": "user", "content": "长" * 500}])
    line = block.splitlines()[-1]
    assert line.startswith("用户：")
    assert line.endswith("…")
    assert len(line) <= HISTORY_ITEM_CHARS + len("用户：") + 1


# ---------- 本地规则优先 ----------


def test_local_rule_wins_over_history(session):
    """「记一下…」是命令式指令，不该因为上一轮聊过别的被改道。"""
    router, provider = _router(session)
    history = [{"role": "user", "content": "看看我的学习诊断"}]
    route = router.route("记一下：今天想明白了一件事", user_id="u1", history=history)

    assert route.source == "local"
    assert route.capability == "knowledge_add"
    assert provider.messages == []   # 根本没调模型


def test_refer_phrase_is_not_swallowed_by_local_rules(session):
    """「再来一次」必须交给模型：本地没有（也不该有）指代规则。"""
    assert _match_local("再来一次") is None
    assert _match_local("那个呢") is None

    router, provider = _router(session)
    route = router.route(
        "再来一次", user_id="u1",
        history=[{"role": "user", "content": "帮我看看有什么冲突"}],
    )
    assert route.source == "model"
    assert provider.messages  # 走的是模型分流


# ---------- copilot 取历史 ----------


def _conv(messages: list[dict]):
    return SimpleNamespace(messages=messages)


def test_recent_dialogue_takes_recent_pairs_in_order():
    conv = _conv([
        {"role": "user", "content": "一"},
        {"role": "assistant", "content": "答一"},
        {"role": "user", "content": "二"},
        {"role": "assistant", "content": "", "card": {"kind": "x"}},   # 空正文跳过
        {"role": "user", "content": "三"},
        {"role": "assistant", "content": "答三"},
    ])
    picked = Copilot._recent_dialogue(conv, turns=2)
    # turns 是「轮」上限，条数上限 = turns*2；空正文那条被跳过，所以凑到 4 条非空
    assert [m["content"] for m in picked] == ["答一", "二", "三", "答三"]


def test_recent_dialogue_skips_system_and_blank():
    conv = _conv([
        {"role": "system", "content": "系统提示"},
        {"role": "user", "content": "  "},
    ])
    assert Copilot._recent_dialogue(conv) == []
