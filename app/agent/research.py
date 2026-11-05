"""资料研究：让模型自己决定搜什么、读哪一页，再给建议。

这个能力存在的理由很具体：**用户要的是外部资料，而知识库里没有**。
以前遇到这种情况只有两条路——要么回一句「这件事做不了」，要么让模型凭记忆
编一份书单。前者把用户的话当没听见，后者更糟：编出来的书名、作者、版本看起来
和真的没区别，用户照单去找才发现不存在。

所以这里把「找」这件事真的做出来：

1. 模型拿到两个只读工具（`web_search` / `web_fetch`），自己决定检索词、要不要读正文；
2. 我们执行工具、把结果原样回灌，模型据此收敛；
3. 最终只是一段**带出处的建议**，加一张来源卡。

**三种失败都必须说出来，不能装作搜到了**（见 `ResearchResult.state`）：

- `no_search`：模型判断不需要联网（问的是概念、或是他自己的东西）——不算失败，
  但卡片上要写「这次没有联网检索」，否则用户会以为看到的是实时资料；
- `search_failed`：搜了，但一条结果都没拿到；
- `failed`：模型调用本身挂了。

「不入库」是硬约定：本模块不 import 任何入库服务，检索结果只活在这次回合里。
用户想留下哪条，自己说一句「记一下」——他是过滤器，质量与版权都留在系统外。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.llm import websearch
from app.llm.exceptions import LLMError
from app.llm.gateway import ModelGateway
from app.llm.prompts import RESEARCH_ADVISOR

logger = logging.getLogger(__name__)

# 工具定义。只读、无副作用，参数尽量窄——模型给多了也没用。
TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "检索网页，返回标题、链接、站点与摘要。用于找教材、课程、论文、官方文档。"
                "一条 query 只表达一个角度，换角度就换一条。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "具体检索词，不要照抄用户整句"},
                    "count": {"type": "integer", "description": "返回条数，1-8，默认 5"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": (
                "抓取单个网页的正文（Markdown）。只有需要核对具体内容（目录、章节、结论）时才用；"
                "只看摘要就能判断的，不要抓。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "检索结果里出现过的完整网址"},
                },
                "required": ["url"],
            },
        },
    },
]

KNOWN_TOOLS: frozenset[str] = frozenset({"web_search", "web_fetch"})

# 预算。三个数字各管一件事：
# - 回合数管「模型能自我纠正几次」，2 轮够走完「搜 → 补搜/读页 → 出结论」；
# - 工具次数管费用上限（检索按成功请求计费），6 次已经能覆盖大多数主题；
# - 来源条数管卡片长度，列出前 8 条，再多用户也不会点。
MAX_ROUNDS = 3
MAX_TOOL_CALLS = 6
MAX_SOURCES = 8


@dataclass
class ResearchResult:
    """一次研究的产物。`sources` 只用于展示，不写库。"""

    text: str
    sources: list[dict] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    tool_calls: int = 0
    state: str = "ok"          # ok | no_search | search_failed | failed
    note: str = ""
    topic: str = ""

    @property
    def searched(self) -> bool:
        return self.tool_calls > 0


def advise(
    *,
    gateway: ModelGateway,
    session: Session,
    user_id: str,
    question: str,
    topic: str = "",
) -> ResearchResult:
    """跑一次「检索 → 归纳 → 建议」。不写任何知识库数据。"""
    prompt = _user_prompt(question, topic)

    if not websearch.available():
        # 没 Key 就搜不了。这里不调模型：让它凭记忆答一份「资料清单」正是要避免的事。
        return ResearchResult(
            text="联网检索没有配置好（缺 DEEPSEEK_API_KEY），这次只能停在这里。",
            state="failed",
            note="没有可用的检索凭据",
            topic=(topic or "").strip(),
        )

    messages: list[dict] = [
        {"role": "system", "content": RESEARCH_ADVISOR.text},
        {"role": "user", "content": prompt},
    ]
    sources: dict[str, dict] = {}
    queries: list[str] = []
    used_calls = 0
    text = ""

    for _round in range(MAX_ROUNDS):
        try:
            completion = gateway.chat(
                task_type="web_research",
                messages=messages,
                tools=TOOLS if used_calls < MAX_TOOL_CALLS else None,
                user_id=user_id,
                session=session,
                prompt_version=RESEARCH_ADVISOR.version,
            )
        except LLMError as exc:
            logger.warning("research 模型调用失败: %s", exc)
            return ResearchResult(
                text="这次没查成——检索链路断了，过一会儿再让我试一次。",
                sources=list(sources.values()),
                queries=queries,
                tool_calls=used_calls,
                state="failed",
                note="模型调用失败",
                topic=(topic or "").strip(),
            )

        text = (completion.text or "").strip()
        calls = [c for c in (completion.tool_calls or []) if c.get("name") in KNOWN_TOOLS]
        if not calls:
            break

        # 预算见底时只执行前几条，并明确告诉模型「剩下的没做」——
        # 让它知道不是工具坏了，而是不能再用，从而直接收敛到结论。
        room = MAX_TOOL_CALLS - used_calls
        todo, dropped = calls[:room], calls[room:]
        used_calls += len(todo)

        messages.append(_assistant_tool_message(completion.text, todo + dropped))
        for index, call in enumerate(todo):
            payload, found = _run_tool(call)
            if found:
                queries.append(found.pop("query", "") or "")
                for item in found.get("_sources") or []:
                    sources.setdefault(item["url"], item)
                found.pop("_sources", None)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": _call_id(call, index),
                    "content": json.dumps(payload, ensure_ascii=False),
                }
            )
        for index, call in enumerate(dropped, start=len(todo)):
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": _call_id(call, index),
                    "content": json.dumps(
                        {"ok": False, "error": "本次检索次数已用完，请基于已有信息作答"},
                        ensure_ascii=False,
                    ),
                }
            )

    if not text:
        text = _followup_text(gateway, session, user_id, messages)

    state = "ok"
    note = ""
    if used_calls == 0:
        state = "no_search"
        note = "这次没有联网检索，建议来自模型已有知识"
    elif not sources:
        state = "search_failed"
        note = "联网检索没有返回结果，下面的建议没有经过外部资料核对"

    return ResearchResult(
        text=text or "这次没整理出可用的建议，换个说法再让我试一次。",
        sources=_cited_sources(text, sources),
        queries=[q for q in queries if q],
        tool_calls=used_calls,
        state=state,
        note=note,
        topic=(topic or "").strip(),
    )


# ---- 内部 ------------------------------------------------------------------


def _cited_sources(text: str, found: dict[str, dict]) -> list[dict]:
    """卡片上只放**正文真正引用过**的来源。

    检索一次会拿到十几条结果，其中大半模型根本没看——原样铺在卡片上，
    用户点进去一半是无关页面，反倒显得这份建议没挑过。
    正文里没出现任何链接时退回全量：至少让他能自己核对。
    """
    if not found:
        return []

    def clean(url: str) -> str:
        return url.split("#", 1)[0].split("?", 1)[0].rstrip("/")

    hit = [item for item in found.values() if clean(item["url"]) and clean(item["url"]) in text]
    picked = hit or list(found.values())
    return picked[:MAX_SOURCES]


def _user_prompt(question: str, topic: str) -> str:
    body = (question or "").strip()
    if topic and topic.strip() and topic.strip() not in body:
        return f"我想了解的主题是：{topic.strip()}\n我的原话是：{body}\n\n请按上面的规矩检索并给建议。"
    return f"我的原话是：{body}\n\n请按上面的规矩检索并给建议。"


def _call_id(call: dict, index: int) -> str:
    """工具调用 id 必须与回灌的 assistant 消息**逐字一致**，否则协议对不上。

    模型偶尔不给 id（走 Mock 或某些兼容层），这里用序号补一个稳定的。
    """
    return str(call.get("id") or f"call_{index}")


def _assistant_tool_message(text: str, calls: list[dict]) -> dict:
    return {
        "role": "assistant",
        "content": text or None,
        "tool_calls": [
            {
                "id": _call_id(call, index),
                "type": "function",
                "function": {
                    "name": call["name"],
                    "arguments": call.get("arguments") or "{}",
                },
            }
            for index, call in enumerate(calls)
        ],
    }


def _parse_args(raw: str) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _run_tool(call: dict) -> tuple[dict, dict | None]:
    """执行一条工具调用。返回 (回灌给模型的结果, 附带信息)。

    任何异常都收敛成 `{"ok": false, "error": ...}` 交给模型——它看到失败会换一个说法，
    而抛异常只会让这一轮白跑。
    """
    args = _parse_args(call.get("arguments"))
    if call["name"] == "web_search":
        query = str(args.get("query") or "").strip()
        result = websearch.search(query, count=int(args.get("count") or 5))
        payload = {
            "ok": result["ok"],
            "results": result["results"],
            "error": result["error"],
        }
        return payload, {"query": query, "_sources": result["results"]}

    if call["name"] == "web_fetch":
        url = str(args.get("url") or "").strip()
        result = websearch.fetch(url)
        payload = {
            "ok": result["ok"],
            "title": result["title"],
            "content": result["content"],
            "error": result["error"],
        }
        # 抓取到的页面本身就是最可靠的来源：即使它没出现在搜索结果里（例如模型
        # 直接读了一篇搜索结果之外的链接），也要记下来，否则出处会漏。
        found = (
            {"query": "", "_sources": [{"title": result["title"], "url": url, "site": "", "date": ""}]}
            if result["ok"]
            else None
        )
        return payload, found

    return {"ok": False, "error": "不支持的工具"}, None


def _followup_text(gateway: ModelGateway, session: Session, user_id: str, messages: list[dict]) -> str:
    """模型只顾调工具、没给结论时，明确要一次总结。

    不带 tools：这一次只允许出文本，避免它又开始检索——预算已经用完了。
    """
    try:
        completion = gateway.chat(
            task_type="web_research",
            messages=[*messages, {"role": "user", "content": "直接给结论和建议，不要再检索。"}],
            user_id=user_id,
            session=session,
            prompt_version=RESEARCH_ADVISOR.version,
        )
    except LLMError as exc:
        logger.warning("research 收尾调用失败: %s", exc)
        return ""
    return (completion.text or "").strip()
