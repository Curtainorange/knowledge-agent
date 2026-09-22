"""统一对话入口的能力路由（「对话即入口」的分流层）。

用户只用说话，这一层负责把这句话分派到某个已有能力上。两条路径：

1. **本地命令式快路径**：明确的下达式说法（「记一下…」「同步微信读书」「生成认知简报」）
   用确定性规则直接判，零模型调用。这类说法在实际使用里占多数，而且判错了用户立刻
   能看出来——把确定性留给它们，比让模型每次都赌一把更划算。
2. **模型分类**：本地规则没命中时才问模型，让它输出 `CapabilityRoute` JSON。

**安全方向是明确的**：分流失败一律回落到通用对话。看不懂就当普通聊天，
比「猜错了去执行一个错动作」安全得多——尤其当能力里包含「写入知识库」这种副作用时。
这与 L1/L4/L5 的既有约定一致：能本地判的绝不调模型，判不准就退到最保守的分支。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from app.llm.gateway import ModelGateway
from app.llm.prompts import AGENT_ROUTE
from app.llm.structure import JsonParseError, parse_structured

logger = logging.getLogger(__name__)

Capability = Literal[
    "l1", "l2", "l3",
    "l4_goal", "l4_plan", "l4_deviation",
    "l5", "knowledge_add", "weread_sync", "books", "chat",
]

KNOWN_CAPABILITIES: tuple[str, ...] = (
    "l1", "l2", "l3",
    "l4_goal", "l4_plan", "l4_deviation",
    "l5", "knowledge_add", "weread_sync", "books", "chat",
)

# 未识别 / 判定不明确时的归宿。**所有兜底都落到这里**。
FALLBACK_CAPABILITY = "chat"

# 目前已经真正接进对话的能力。其余能力路由层认得、但由 copilot 回一张「去哪儿」的引导卡，
# 而不是假装做完了——「路由认得」与「能力已接入」必须分开，否则用户会以为功能坏了。
WIRED_CAPABILITIES: frozenset[str] = frozenset({
    "l1", "l2", "l3", "l4_goal", "l4_plan", "l4_deviation", "l5",
    "knowledge_add", "weread_sync", "books", "chat",
})


@dataclass(frozen=True)
class CapabilitySpec:
    """能力目录项：对话入口要怎么向用户呈现这个能力。

    `example` 不只是文案——它是快捷入口发出的原文，**必须能被本地规则命中**，
    否则「点一下按钮」会退化成一次模型调用，结果还不确定。新增能力时先补规则，
    再把示例写进来（`tests/test_agent_router.py` 会逐条验证这件事）。

    `href` 是该能力的**原页面**：未接入对话时它就是引导卡的落点；
    已接入对话后仍留着，用作卡片上的「在原页面查看」深链（完整视图比卡片更全）。
    """

    capability: str
    label: str
    example: str
    href: str = ""


CAPABILITY_CATALOG: tuple[CapabilitySpec, ...] = (
    CapabilitySpec("l1", "按线索找回", "之前存的那段讲查询优化的内容"),
    CapabilitySpec("knowledge_add", "记一条", "记一下：B+树更适合范围查询 #数据库"),
    CapabilitySpec("l2", "冲突检测", "扫描知识冲突", "/conflicts.html"),
    CapabilitySpec("l3", "认知简报", "生成认知简报", "/brief.html"),
    CapabilitySpec("l4_goal", "学习目标", "帮我定一个学习目标：三个月掌握数据分析", "/l4.html"),
    CapabilitySpec("l4_plan", "拆周计划", "生成周计划", "/l4.html"),
    CapabilitySpec("l4_deviation", "偏离检查", "检查我有没有偏离计划", "/l4.html"),
    CapabilitySpec("l5", "健康诊断", "诊断我的学习状态", "/l5.html"),
    CapabilitySpec("weread_sync", "同步微信读书", "同步微信读书", "/books.html"),
    CapabilitySpec("books", "书架", "我的书架里有什么", "/books.html"),
)


def capability_spec(name: str) -> CapabilitySpec | None:
    """按能力名取目录项（引导卡与快捷入口共用，避免两处各写一份文案）。"""
    for spec in CAPABILITY_CATALOG:
        if spec.capability == name:
            return spec
    return None


class CapabilityRoute(BaseModel):
    """一次分流的结论。

    `source` 由路由层填写，不属于模型输出契约；模型若漏给或乱给都会被 `_coerce` 收拾干净。
    """

    capability: Capability = FALLBACK_CAPABILITY
    args: dict = Field(default_factory=dict)
    confidence: float = 0.0
    reason: str = ""
    source: str = "model"  # local | model | fallback

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data):
        """把模型的自由输出收敛成合法路由。

        分流是「每次都跑」的链路，模型偶尔给个 `confidence: "高"`、`args: []` 或者
        编一个不存在的能力名，都不该让整轮对话失败——这里一律就地修正，
        修不动就退到通用对话（`source` 保持 model，由调用方决定怎么记日志）。
        """
        if not isinstance(data, dict):
            return {"capability": FALLBACK_CAPABILITY, "confidence": 0.0}
        fixed = dict(data)
        if not isinstance(fixed.get("args"), dict):
            fixed["args"] = {}
        try:
            fixed["confidence"] = min(1.0, max(0.0, float(fixed.get("confidence") or 0.0)))
        except (TypeError, ValueError):
            fixed["confidence"] = 0.0
        name = str(fixed.get("capability") or "").strip()
        fixed["capability"] = name if name in KNOWN_CAPABILITIES else FALLBACK_CAPABILITY
        if not isinstance(fixed.get("reason"), str):
            fixed["reason"] = ""
        return fixed


# ---- 本地命令式规则 --------------------------------------------------------
#
# 只认「下达式」说法，不做语义理解——语义交给模型。刻意写窄：宁可漏判（落到模型），
# 也不要误判（把闲聊当成「扫描冲突」执行）。

# 「记一条内容」的引导词。命中后会剥掉，剩下的部分才是正文。
# 按长度倒序匹配：否则「帮我记录一下」会被更短的「帮我记」截走，正文里剩个「录一下」。
_NOTE_VERBS = tuple(sorted((
    "帮我记录一下", "帮我记一下", "帮我记一条", "帮我记录", "帮我记",
    "记录下来", "记录一下", "记录一条",
    "记一下", "记一条", "记一记", "记下", "存一下", "收录一条",
), key=len, reverse=True))

_SCAN_WORDS = ("扫描", "扫一下", "扫一遍", "检测", "检查", "查一下", "看看", "有没有", "找找")
_CONFLICT_WORDS = ("冲突", "矛盾", "打架", "不一致")

# L4 拆成三个能力（而不是一个 l4 + 子参数），因为**异步是按能力名判定的**，
# 而这三件事的速度差了一个数量级：定目标是本地写库、拆计划调一次模型、
# 偏离检查在 reasoning=on 上跑。合成一个能力就没法给它们各自选同步还是异步。
_GOAL_VERBS = ("定", "设", "立", "制定", "换一个", "我想学", "我要学", "帮我规划")
_PLAN_WORDS = ("周计划", "学习计划", "计划")
_PLAN_VERBS = ("生成", "拆", "重排", "重新排", "安排", "规划", "制定")
# 偏离检查要「偏离类词」或「计划 + 检查类动词」；后者刻意不含「生成/拆」，
# 否则「生成周计划」会被抢走
_DEVIATION_WORDS = ("偏离", "跑偏", "没按计划", "没跟上", "进度落后", "执行情况")
_DEVIATION_VERBS = ("检查", "看看", "有没有按", "怎么样", "如何")
# 纯查看（不产生副作用）也归 l4_goal：它和「定目标」都是同步、不调模型、围绕同一份数据，
# 为一次本地查询单开一个能力不值得
_PLAN_VIEW_WORDS = ("我的计划", "计划进展", "计划是什么", "计划怎么样", "目标有哪些", "我的目标")

# 「定目标」的引导词：剥掉之后剩下的才是目标描述。按长度倒序，避免长词被短词截断。
_GOAL_TRIGGERS = tuple(sorted((
    "帮我定一个学习目标", "帮我定个学习目标", "帮我制定学习目标", "帮我设定学习目标",
    "帮我定一个目标", "帮我定个目标", "帮我规划一下", "帮我规划",
    "定一个学习目标", "定个学习目标", "设置学习目标", "设定学习目标",
    "定一个目标", "定个目标", "我想学", "我想学习", "我要学", "换一个目标",
), key=len, reverse=True))

_RECALL_PREFIXES = ("帮我找", "找一下", "找找", "找回", "搜一下", "之前存", "之前记", "记得我")
_RECALL_MARKERS = ("那条", "那本", "那段", "那篇", "记得我存", "记得我记")

_BOOK_WORDS = ("书架", "阅读记录", "阅读日志", "读了什么", "读了哪些")
_SYNC_WORDS = ("微信读书", "微信阅读", "weread")

TAG_PATTERN = re.compile(r"#([^\s#]{1,16})")
_TITLE_MAX = 40
_GOAL_MAX = 256


def _has_any(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)


def parse_note(text: str) -> dict:
    """把「记一下：XXX #标签」拆成录入知识库需要的 title / content / tags。

    触发词只剥掉一次且必须在开头；`#标签` 从正文里剔除后去重保序。
    正文为空说明用户其实没说记什么，调用方应据此拒绝写入，别落一条空条目。
    """
    body = (text or "").strip()
    for verb in _NOTE_VERBS:
        if body.startswith(verb):
            body = body[len(verb):].lstrip("：:，,。.、 ")
            break

    tags: list[str] = []
    for found in TAG_PATTERN.findall(body):
        if found not in tags:
            tags.append(found)
    body = TAG_PATTERN.sub("", body).strip()

    if not body:
        return {"title": "", "content": "", "tags": tags}

    first_line = next((line.strip() for line in body.splitlines() if line.strip()), body)
    return {"title": first_line[:_TITLE_MAX], "content": body, "tags": tags}


def parse_goal(text: str) -> str:
    """从「帮我定一个学习目标：三个月掌握数据分析」里剥出目标描述。

    和 `parse_note` 同一套路：剥掉开头的引导词与紧随的分隔符，剩下的就是正文。
    剥空了说明用户没说清要定什么目标，调用方据此反问一句，而不是建一个空目标。
    """
    body = (text or "").strip()
    for trigger in _GOAL_TRIGGERS:
        if body.startswith(trigger):
            body = body[len(trigger):]
            break
    return body.lstrip("：:，,。.、 ").strip()[:_GOAL_MAX]


def _match_local(text: str) -> CapabilityRoute | None:
    """命令式快路径。命中返回路由，未命中返回 None（交给模型）。"""
    def hit(capability: str, reason: str, **args) -> CapabilityRoute:
        return CapabilityRoute(
            capability=capability, args=args, confidence=1.0, reason=reason, source="local"
        )

    # 1. 记一条内容 —— 优先级最高：它带副作用，且开头词就是强信号
    if text.startswith(_NOTE_VERBS):
        return hit("knowledge_add", "以「记一条」类引导词开头")

    # 2. 同步微信读书
    if _has_any(text, _SYNC_WORDS) and ("同步" in text or "拉取" in text):
        return hit("weread_sync", "识别到同步微信读书")

    # 3. L2 冲突 —— 要求「扫描类动词 + 冲突类名词」同时出现，
    #    避免误伤「记一下：冲突检测的心得」这类只是提到冲突一词的说法
    if _has_any(text, _CONFLICT_WORDS) and _has_any(text, _SCAN_WORDS):
        return hit("l2", "识别到扫描冲突")

    # 4. L3 认知简报
    if "简报" in text:
        return hit("l3", "识别到生成简报")

    # 5. L5 健康诊断
    if _has_any(text, ("诊断", "偏误", "健康报告", "学得怎么样", "学习怎么样")):
        return hit("l5", "识别到健康诊断")

    # 6. L4-偏离检查 —— 放在「拆计划」之前：两者都含「计划」，
    #    但「检查我有没有偏离计划」里的动作词是「检查」而不是「生成/拆」
    if _has_any(text, _DEVIATION_WORDS) or (
        "计划" in text and _has_any(text, ("检查", "有没有按"))
    ):
        return hit("l4_deviation", "识别到检查计划执行偏离")

    # 7. L4-拆周计划
    if _has_any(text, _PLAN_WORDS) and _has_any(text, _PLAN_VERBS):
        return hit("l4_plan", "识别到生成或重排周计划")

    # 8. L4-定目标 / 查看目标与计划（同步入口）
    if "目标" in text and _has_any(text, _GOAL_VERBS):
        return hit("l4_goal", "识别到设定学习目标", intent="create", goal=parse_goal(text))
    if _has_any(text, _PLAN_VIEW_WORDS):
        return hit("l4_goal", "识别到查看当前目标与计划", intent="view")

    # 9. L1 按线索找回 —— 放在 books 之前：
    #    「帮我找书架里那本讲索引的书」的意图是 L1，不该被「书架」抢走
    if text.startswith(("找", "搜")) or _has_any(text, _RECALL_PREFIXES + _RECALL_MARKERS):
        return hit("l1", "识别到按线索找回")

    # 10. 书架 / 阅读记录
    if _has_any(text, _BOOK_WORDS):
        return hit("books", "识别到查看书架")

    return None


class CapabilityRouter:
    """把一句话变成一条 `CapabilityRoute`。`gateway` 可注入（测试用假件）。"""

    def __init__(self, gateway: ModelGateway, session: Session | None = None) -> None:
        self._gateway = gateway
        self._session = session

    def route(
        self, message: str, *, user_id: str = "anonymous", allow_model: bool = True
    ) -> CapabilityRoute:
        """分流一次。

        `allow_model=False` 用于「上一轮 L1 还在等补充」这种状态：此时用户的话极可能
        就是对追问的回答，再花一次模型调用去猜意图是浪费，而且猜错会打断澄清。
        命令式规则仍然生效——用户依然能中途插一条「记一下…」。
        """
        text = (message or "").strip()
        if not text:
            return CapabilityRoute(
                capability=FALLBACK_CAPABILITY, confidence=0.0,
                reason="空输入", source="local",
            )

        local = _match_local(text)
        if local is not None:
            return local

        if not allow_model:
            return CapabilityRoute(
                capability=FALLBACK_CAPABILITY, confidence=0.0,
                reason="会话处于澄清态且未命中命令式规则，按补充回答处理", source="local",
            )

        return self._model_route(text, user_id=user_id)

    def _model_route(self, text: str, *, user_id: str) -> CapabilityRoute:
        messages = [
            {"role": "system", "content": AGENT_ROUTE.text},
            {"role": "user", "content": f"用户这句话是：\n{text}\n\n请输出路由 JSON。"},
        ]
        completion = self._gateway.chat(
            task_type="capability_routing",
            messages=messages,
            user_id=user_id,
            session=self._session,
            prompt_version=AGENT_ROUTE.version,
            json_model=CapabilityRoute,
        )
        try:
            route = parse_structured(completion.text, validator=lambda d: CapabilityRoute(**d))
        except JsonParseError as exc:
            logger.warning("capability routing parse failed, fallback to chat: %s", exc)
            return CapabilityRoute(
                capability=FALLBACK_CAPABILITY, confidence=0.0,
                reason="分流输出无法解析，回落通用对话", source="fallback",
            )
        route.source = "model"
        return route
