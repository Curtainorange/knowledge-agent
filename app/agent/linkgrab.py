"""贴链接即存：把一个网址抓成正文，再存成一条知识。

**为什么单独一个模块。** 这条规则现在有两个入口：计划卡上的「勾选入库」，
和对话里直接贴一个链接。同一条规则写两遍，两边对「标题取哪个」「抓不到怎么办」
迟早给出不同答案——而用户看到的会是两套口径（复盘：「同一条规则实现了两遍」）。
所以抓取 + 入库只在这里实现一次，两个入口都调它。

**三条边界，都是「别替用户做主」：**

1. **抓不抓由模型判，不由这里判。** 「存这个链接」和「问你关于这个链接的看法」
   只有读懂语义才分得清——「你觉得这篇怎么样（附链接）」比「存一下」还长，
   任何字数、关键词规则都会把它判反。所以主路径是路由层让模型给 `args.url`；
   本模块的 `is_url_only` 只用于**先验**与**模型不可用时的兜底**，
   不参与主路径决定（见 `copilot._declared_links`）。
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

# 剥掉链接后剩余内容的容忍长度。低于它就认为「这句话就是在让我存这个链接」。
#
# **这个数字只用于先验和兜底，主路径不靠它**（主路径是模型给 `args.url`）。
# 为什么要收紧到 4：先验一旦标错，模型会有盲从的可能——「你觉得这篇怎么样」
# 是 8 个字，若算进「纯链接」，就会把用户的提问当成「存链接」执行掉，
# 那是这个功能最不能犯的错。反方向的代价只是多问一次模型（本来就每轮都问）。
_RESIDUE_TOLERANCE = 4


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

    **只用于先验和兜底，不是主路径的判官。** 判断「用户想不想让我存这个网页」
    要看语义：同样一句话，加个「你觉得这篇怎么样」意思就完全相反，而字数更多
    反而更像「在提问」。所以真正决定抓不抓的是模型给的 `args.url`。

    这里的存在理由只有一个：模型不可用（或处在澄清态不问模型）时，得有个
    保守的兜底，否则用户贴个链接就什么都不会发生。
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


def user_note(text: str) -> str:
    """取出用户自己写的那部分（去掉网址、引导词、标点后还剩的实质内容）。

    用途是**决定要不要把用户原话一起存进正文**，不是决定抓不抓。这个区别很重要：
    判断错了的后果不对称——该加没加，用户写的那句话就丢了（他一定会发现）；
    不该加却加了，正文开头多一行他自己的话（无害，甚至更好检索）。
    所以这里的阈值可以宽，只要"确实写了点东西"就够了。
    """
    residue = text or ""
    for url in extract_urls(residue):
        residue = residue.replace(url, " ")
    for verb in _LEAD_VERBS:
        residue = residue.replace(verb, " ")
    residue = re.sub(r"[\s，,。.、；;：:！!？?（）()【】\[\]「」\"'~—-]+", "", residue)
    return residue if len(residue) >= 2 else ""


def save_url(
    *, session: Session, user_id: str, url: str, title_hint: str = "",
    tags: list[str] | None = None, note: str = "",
) -> dict:
    """抓 `url` 的正文并入库。返回 `{ok, url, title, chars, item, error}`。

    不抛异常：调用方要么在对话里（失败要说人话），要么在批量入库里（一条失败
    不该让整批停住）。失败原因原样带出，由调用方决定说多少。

    `item` 是入库成功后的 `KnowledgeItem`，给调用方建卡片用（卡片要 item.id 才能跳转）。

    `note` 是用户那句话。**他自己写了东西时，原话和网页正文一起存**——
    抓取只该补充内容，不该把他写的判断冲掉（「这篇 benchmark 我有疑问」这种话
    恰恰是以后最该被检索到的东西）。
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

    if user_note(note):
        # 用户原话放最前：他写的判断比网页正文更能代表「我为什么存这条」。
        content = f"{note.strip()}\n\n---\n\n{content}"

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
