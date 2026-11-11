"""推送通道发送器：把 PushService 生成的提醒送到「站内」之外的出口。

延续 ADR-14「推送是旁路」：sender 尽力而为——失败只记日志、返回 False，
绝不抛异常打断调用方；也不写库、不碰 PushJob 状态机（PushJob 永远是站内通知的权威载体）。

微信通道（opt-in，默认关，见 settings.push_wechat_enabled）：Server酱 微信推送（需要
SendKey），手机微信直接收。电脑端不再弹系统通知，改为打开项目时的网页内弹窗
（纯前端，见 web/assets/app.js 的 CC.notify）。
"""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

WECHAT_URL = "https://sctapi.ftqq.com/{sendkey}.send"

# Server酱 卡片标题硬限制 32 字（超出被服务端截断，换行会让整条推送报错）
TITLE_MAX = 32


def _one_line(text: str) -> str:
    """压成单行：Server酱 的 title 不能含换行，正文也可能带换行。"""
    return " ".join((text or "").split())


def compose_title(title: str, body: str) -> str:
    """把标题和正文压成一条 ≤32 字的标题。

    为什么标题要带正文摘要：Server酱 **免费版卡片只显示标题、不显示 desp**
    （官方对比表：免费「仅显示标题」，订阅才「显示标题和内容」）。只发 title 的话，
    手机上就是一条「只有标题、点开也没内容」的空推送（2026-11-11 用户现场反馈）。
    所以正文摘要必须挤进标题，desp 仍然照发——付费账号可直接看到完整正文。
    """
    head = _one_line(title)
    text = _one_line(body)
    if not text or text in head:
        merged = head or text
    elif head and head in text:
        # 正文本身已包含标题（如周简报正文以标题开头），避免重复拼接
        merged = text
    else:
        merged = f"{head}｜{text}" if head else text
    if not merged:
        return "认知副驾"
    if len(merged) > TITLE_MAX:
        merged = merged[: TITLE_MAX - 1] + "…"
    return merged


def send_wechat(
    title: str,
    body: str,
    *,
    sendkey: str = "",
    timeout: float = 20.0,
    http: httpx.Client | None = None,
) -> bool:
    """Server酱 微信推送。sendkey 为空即未启用，直接返回 False。

    Server酱 约定：title 必填、≤32 字、不能含换行；desp 是 Markdown 正文。
    返回 JSON 的 `code == 0` 表示成功。免费额度每天 5 条。

    `http` 可注入（测试用 httpx.MockTransport），风格对齐 app/weread/client.py。
    """
    if not sendkey:
        return False
    if not sendkey.startswith("SCT"):
        logger.warning("Server酱 SendKey 应以 SCT 开头，当前值无法识别，跳过")
        return False
    if sendkey.lower().startswith("sctp"):
        # sctp 是企业版 Key，走不同域名（push.ft07.com），当前未接入
        logger.warning("暂不支持 sctp 开头的企业版 SendKey，跳过")
        return False

    client = http or httpx.Client(timeout=timeout)
    try:
        resp = client.post(
            WECHAT_URL.format(sendkey=sendkey),
            data={"title": compose_title(title, body), "desp": body},
        )
        if resp.status_code == 200:
            try:
                ok = resp.json().get("code") == 0
            except ValueError:
                ok = False
        else:
            ok = False
        if not ok:
            logger.warning(
                "Server酱 返回异常 status=%s body=%s", resp.status_code, resp.text[:200]
            )
        return ok
    except httpx.HTTPError as exc:
        logger.warning("Server酱 请求失败：%s", exc)
        return False
    finally:
        if http is None:
            client.close()
