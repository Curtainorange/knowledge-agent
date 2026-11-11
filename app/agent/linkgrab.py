"""贴链接即存：把一个网址抓成正文，再存成一条知识。

**为什么单独一个模块。** 这条规则现在有两个入口：计划卡上的「勾选入库」，
和对话里直接贴一个链接。同一条规则写两遍，两边对「标题取哪个」「抓不到怎么办」
迟早给出不同答案——而用户看到的会是两套口径（复盘：「同一条规则实现了两遍」）。
所以抓取 + 入库只在这里实现一次，两个入口都调它。

**三条边界，都是「别替用户做主」：**

1. **只抓整句基本就是链接的那些。** 用户写「记一下：这篇文章不错（附链接）
   但观点有问题」时，他要存的是自己那句话；抓取会把他写的东西丢掉。
   所以正文里除链接外还剩多少字，决定了走不走抓取（见 `is_url_only`）。
2. **抓不到就说抓不到，不入库。** 存一条只有网址、没有正文的条目，检索时
   永远命不中——那不是「已保存」，是给库里塞垃圾。
3. **标题优先用网页标题。** 用户看到「用 bge-m3 做中文检索」才知道这条是什么；
   看到一串 URL 什么都不知道。

出网走的是 `websearch.fetch`（DeepSeek 官方抓取接口），**没有新增出网主机**——
用户给什么链接都不会让本机直连那个地址，`url` 只是作为参数发给已登记的那台服务器。
"""
from __future__ import annotations

import logging
import re

from sqlalchemy.orm import Session

from app.ingestion.service import SOURCE_MANUAL, IngestionService
from app.llm import websearch

logger = logging.getLogger(__name__)

# 正文里认得的网址形态。刻意只认 http(s)：`example.com` 这种裸域名与普通英文
# 无从区分（「vue.js」「我买了 x.y」），误判一次就会去抓一个不存在的东西。
URL_PATTERN = re.compile(r"https?://[^\s<>\"'`）)】\]，。；、]+")

# 入库时的正文上限。比回灌模型的 4000 字宽得多：给模型看只要要点，
# 存进库里的是用户以后要检索、要通读的东西，只存开头等于存了半篇。
INGEST_CONTENT_CHARS = 12000

# 剥掉链接后剩余内容的容忍长度。低于它就认为「这句话就是在让我存这个链接」，
# 剩下的那几个字算备注（「讲检索的」），不算要被保存的正文。
#
# 为什么是 8：这个数字不精确，取值方向是「宁可退回原文存」。多抓一次只是少存
# 用户几个字的备注不易察觉；错抓一次则是把他写的那句话整段丢掉——后者用户
# 一定会发现，而且找不回来。中文里 8 个字已经能装下一句完整评价
# （「和我的理解差挺多」就是 8 个），过长的备注一律按原文录入。
_RESIDUE_TOLERANCE = 8


def extract_urls(text: str) -> list[str]:
    """按出现顺序取出文本里的网址，去重。"""
    seen: list[str] = []
    for raw in URL_PATTERN.findall(text or ""):
        url = raw.strip().rstrip(".,;:!?")
        if url and url not in seen:
            seen.append(url)
    return seen


def is_url_only(text: str) -> bool:
    """这句话是不是「基本只有一个链接」（去掉链接后几乎没剩什么）。

    `_RESIDUE_TOLERANCE` 不是精确阈值，是一条保守的边界：宁可让「链接 + 一句话」
    退回按原文存（用户的话不会丢），也不要在有正文的时候擅自抓取、把他的备注抹掉。
    """
    urls = extract_urls(text)
    if not urls:
        return False
    residue = text or ""
    for url in urls:
        residue = residue.replace(url, " ")
    # 只算「有意义的字符」：标点与引导词不算内容，否则「记一下：<链接>」永远不算纯链接
    residue = re.sub(r"[\s，,。.、；;：:！!？?（）()【】\[\]「」\"'~—-]+", "", residue)
    for verb in _LEAD_VERBS:
        residue = residue.replace(verb, "")
    return len(residue) <= _RESIDUE_TOLERANCE


# 与 router 的「记一条」引导词同源。这里复制一份而不是 import，是为了避免
# `agent.linkgrab → agent.router → agent.turns` 的循环引用；两边都是常量，
# 且 `is_url_only` 只把它们当噪音剔除，宽一点窄一点都不会改变判定结果的性质。
_LEAD_VERBS = (
    "帮我记录一下", "帮我记一下", "帮我记一条", "帮我记录", "帮我记",
    "记录下来", "记录一下", "记录一条", "记一下", "记一条", "记一记", "记下",
    "存一下", "收录一条", "收藏一下", "收藏", "保存一下", "保存", "存下来",
)


def save_url(
    *, session: Session, user_id: str, url: str, title_hint: str = "", tags: list[str] | None = None
) -> dict:
    """抓 `url` 的正文并入库。返回 `{ok, url, title, chars, item, error}`。

    不抛异常：调用方要么在对话里（失败要说人话），要么在批量入库里（一条失败
    不该让整批停住）。失败原因原样带出，由调用方决定说多少。

    `item` 是入库成功后的 `KnowledgeItem`，给调用方建卡片用（卡片要 item.id 才能跳转）。
    """
    target = (url or "").strip()
    fetched = websearch.fetch(target, max_chars=INGEST_CONTENT_CHARS)
    if not fetched.get("ok"):
        return {
            "ok": False, "url": target, "title": "", "chars": 0, "item": None,
            "error": str(fetched.get("error") or "没取到正文"),
        }

    content = str(fetched.get("content") or "").strip()
    if not content:
        return {
            "ok": False, "url": target, "title": "", "chars": 0, "item": None,
            "error": "这一页没有正文",
        }

    title = (title_hint or "").strip() or str(fetched.get("title") or "").strip() or target
    service = IngestionService(session, _embedding())
    # tags 里带上「联网资料」：让「这条是从网上存的」在库里看得见，
    # 也让用户以后能按来源收窄检索。用户自己的 #标签 一并保留。
    merged: list[str] = []
    for tag in [*(tags or []), "联网资料"]:
        text = str(tag).strip()
        if text and text not in merged:
            merged.append(text)
    item = service.add_knowledge(
        user_id=user_id,
        title=title[:200],
        content=content,
        source=SOURCE_MANUAL,
        tags=merged,
    )
    return {
        "ok": True, "url": target, "title": item.title,
        "chars": len(content), "item": item, "error": "",
    }


def _embedding():
    """惰性构建向量模型：模块导入时不加载（首次加载可能几十秒）。

    测试里换 `app.agent.linkgrab._embedding` 即可注入假模型，与 turns 的做法一致。
    """
    from app.retrieval.embedding import build_embedding

    return build_embedding()
