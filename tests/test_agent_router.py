"""对话入口的能力路由测试（全 Mock，不触网）。

两件事最值得钉死：

1. **快捷入口的示例话术必须能被本地规则命中**。示例是「点一下就发出」的按钮文案，
   一旦规则改了、文案没跟着改，按钮就会退化成一次模型调用——结果不确定，
   而且是静默退化（用户只觉得「点了没反应/答非所问」）。这里逐条把它钉住。
2. **分流失败必须回落到通用对话**。分流链路里挂着「写入知识库」这种有副作用的能力，
   猜错方向的代价远大于「看不懂就当聊天」。
"""
from __future__ import annotations

import json

import pytest

from app.agent.copilot import Copilot
from app.agent.router import (
    CAPABILITY_CATALOG,
    KNOWN_CAPABILITIES,
    WIRED_CAPABILITIES,
    CapabilityRoute,
    CapabilityRouter,
    _match_local,
    parse_note,
)
from app.llm.completion import Completion
from app.llm.gateway import ModelGateway
from app.llm.provider import LLMProvider


class FakeProvider(LLMProvider):
    """按预设 JSON 顺序回放的路由供应商（用于覆盖「模型分流」那条路径）。"""

    def __init__(self, rows: list[str]) -> None:
        self.rows = list(rows)
        self.task_types: list[str] = []

    def chat(
        self, *, model, reasoning, messages, tools=None, response_format=None, task_type="default"
    ) -> Completion:
        self.task_types.append(task_type)
        text = self.rows.pop(0) if self.rows else "不是 JSON"
        return Completion(
            text=text, prompt_tokens=1, completion_tokens=1, finish_reason="stop",
            model=model, reasoning=reasoning,
        )


def _router(session, rows: list[str] | None = None) -> tuple[CapabilityRouter, FakeProvider]:
    provider = FakeProvider(rows or [])
    return CapabilityRouter(ModelGateway(provider=provider), session), provider


# ---- 快捷入口示例话术：必须本地命中 -----------------------------------------


@pytest.mark.parametrize("spec", CAPABILITY_CATALOG, ids=[s.capability for s in CAPABILITY_CATALOG])
def test_catalog_example_routes_locally(spec):
    """每条快捷入口的示例话术都要被本地规则命中，且落到自己那个能力上。"""
    route = _match_local(spec.example)
    assert route is not None, f"{spec.label} 的示例话术没命中任何本地规则：{spec.example!r}"
    assert route.capability == spec.capability, (
        f"{spec.label} 的示例话术被分到了 {route.capability}"
    )
    assert route.source == "local"


def test_catalog_and_wiring_are_consistent(session):
    """目录、路由、分发三处的能力清单必须一致——否则按钮点下去就是空转。"""
    copilot = Copilot(ModelGateway(provider=FakeProvider([])), session)
    dispatchable = set(copilot._handlers())

    assert WIRED_CAPABILITIES <= dispatchable, (
        f"标为已接入但没实现分发：{WIRED_CAPABILITIES - dispatchable}"
    )
    # 目录覆盖除 chat 以外的全部已知能力（chat 不需要快捷入口）
    catalogued = {spec.capability for spec in CAPABILITY_CATALOG}
    expected = set(KNOWN_CAPABILITIES) - {"chat"}
    assert catalogued == expected, (
        f"目录与能力清单不一致：多 {catalogued - expected}，缺 {expected - catalogued}"
    )
    # 目前全部已接入；仍留这条是为了「将来新增能力时忘了接线」能被拦住
    for spec in CAPABILITY_CATALOG:
        if spec.capability not in WIRED_CAPABILITIES:
            assert spec.href, f"{spec.label} 未接入对话，又没给原页面地址，引导卡会指向空"


# ---- 本地规则 ---------------------------------------------------------------


def test_note_routes_to_knowledge_add():
    route = _match_local("记一下：B+树更适合范围查询 #数据库")
    assert route.capability == "knowledge_add"


def test_sync_routes_to_weread():
    assert _match_local("同步微信读书").capability == "weread_sync"
    # 只有平台名、没有同步意图 → 不该抢走别的意图
    assert _match_local("我最近在看微信读书") is None


def test_conflict_needs_scan_verb():
    """「冲突」这个词单独出现不算扫描意图，否则「找讲冲突的内容」会被误判。"""
    assert _match_local("扫描知识冲突").capability == "l2"
    assert _match_local("记一下：冲突检测的三种类型").capability == "knowledge_add"
    assert _match_local("帮我找那篇讲冲突的笔记").capability == "l1"


def test_goal_word_does_not_steal_recall_intent():
    """「目标」是 L4 的词，但「找关于目标的笔记」是 L1 的意图——泛动词不能算规划动词。"""
    assert _match_local("帮我定一个学习目标").capability == "l4_goal"
    assert _match_local("帮我找关于目标的笔记").capability == "l1"


def test_l4_three_ways_do_not_cross():
    """L4 拆成三个能力后，三者不能互相抢。

    这是拆分带来的新风险：三者的触发词都绕着「计划/目标」转，
    规则一糊就会把「生成周计划」判成「检查偏离」。
    """
    assert _match_local("生成周计划").capability == "l4_plan"
    assert _match_local("重新生成周计划").capability == "l4_plan"
    assert _match_local("检查我有没有偏离计划").capability == "l4_deviation"
    assert _match_local("计划执行情况怎么样").capability == "l4_deviation"
    # 纯查看走同步入口，不触发任何写操作
    view = _match_local("我的计划进展如何")
    assert view.capability == "l4_goal" and view.args["intent"] == "view"


def test_books_not_stolen_by_recall():
    assert _match_local("我的书架里有什么").capability == "books"


def test_ambiguous_text_has_no_local_rule():
    """判不出来必须返回 None，交给模型——绝不能在本地硬猜一个能力。"""
    for text in ("今天天气不错", "谢谢", "帮我看看这个", "最近怎么样"):
        assert _match_local(text) is None, text


# ---- 记一条：参数抽取 -------------------------------------------------------


def test_parse_note_extracts_title_content_and_tags():
    note = parse_note("记一下：B+树更适合范围查询 #数据库 #索引")
    assert note["title"] == "B+树更适合范围查询"
    assert note["content"] == "B+树更适合范围查询"   # 标签已从正文剔除
    assert note["tags"] == ["数据库", "索引"]


def test_parse_note_strips_longest_verb_first():
    """引导词要按最长匹配剥。

    否则「帮我记录一下…」会被更短的「帮我记」截走，正文里剩个「录一下」。
    """
    note = parse_note("帮我记录一下：今天想通了递归的本质")
    assert note["content"] == "今天想通了递归的本质"


def test_parse_note_keeps_multiline_content():
    note = parse_note("记一下：第一条结论\n第二条支撑理由")
    assert note["title"] == "第一条结论"
    assert "第二条支撑理由" in note["content"]


def test_parse_note_empty_body_is_detectable():
    """只说了「记一下」没说记什么 → 正文为空，调用方据此拒绝写入。"""
    assert parse_note("记一下")["content"] == ""
    assert parse_note("记一下 #标签")["content"] == ""


# ---- 模型分流 ---------------------------------------------------------------


def test_model_path_is_used_for_ambiguous_text(session):
    router, provider = _router(
        session,
        ['{"capability":"l3","args":{},"confidence":0.8,"reason":"想看简报"}'],
    )
    route = router.route("帮我看看我最近的知识结构", user_id="u1")
    assert route.capability == "l3"
    assert route.source == "model"
    assert provider.task_types == ["capability_routing"]


def test_model_path_skipped_when_disabled(session):
    """澄清态下不调模型：用户这句话极可能就是对追问的回答，再猜一次既费钱又可能打断澄清。"""
    router, provider = _router(session, ['{"capability":"l3","args":{},"confidence":0.9}'])
    route = router.route("数据库索引", allow_model=False)
    assert route.capability == "chat"
    assert route.source == "local"
    assert provider.task_types == []


def test_parse_failure_falls_back_to_chat(session):
    """分流输出无法解析 → 通用对话（**不是**随便挑一个能力去执行）。"""
    router, _ = _router(session, ["我觉得你想找东西，但我不打算输出 JSON"])
    route = router.route("嗯……那个", user_id="u1")
    assert route.capability == "chat"
    assert route.source == "fallback"


def test_unknown_capability_is_coerced_to_chat():
    """模型编一个不存在的能力名 → 收敛成通用对话，而不是让整轮对话 500。"""
    route = CapabilityRoute(capability="recall_something", args=[], confidence="高")
    assert route.capability == "chat"
    assert route.args == {}
    assert route.confidence == 0.0


def test_confidence_is_clamped():
    assert CapabilityRoute(capability="l1", confidence=3.5).confidence == 1.0
    assert CapabilityRoute(capability="l1", confidence=-2).confidence == 0.0


def test_known_capabilities_cover_catalog():
    for spec in CAPABILITY_CATALOG:
        assert spec.capability in KNOWN_CAPABILITIES, spec.capability


def test_empty_message_stays_local(session):
    router, provider = _router(session, ['{"capability":"l1","args":{}}'])
    route = router.route("   ")
    assert route.capability == "chat"
    assert route.source == "local"
    assert provider.task_types == []


def test_cost_is_recorded_for_routing(session):
    """分流也走网关，成本必须落库——否则「每轮一次」的调用会变成看不见的开销。"""
    from app.domain.models.cost_log import CostLog

    router, _ = _router(session, ['{"capability":"chat","args":{},"confidence":0.5}'])
    router.route("帮我看看这个", user_id="u1")
    session.flush()
    rows = session.query(CostLog).filter(CostLog.task_type == "capability_routing").all()
    assert len(rows) == 1
    # 版本号跟着 AGENT_ROUTE 走：改了提示词必须 bump，否则成本日志追溯不到当初发的是哪一版
    from app.llm.prompts import AGENT_ROUTE

    assert rows[0].prompt_version == AGENT_ROUTE.version
