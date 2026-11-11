"""推送通道发送器：把 PushService 生成的提醒送到「站内」之外的出口。

延续 ADR-14「推送是旁路」：每个 sender 都尽力而为——失败只记日志、返回 False，
绝不抛异常打断调用方；也不写库、不碰 PushJob 状态机（PushJob 永远是站内通知的权威载体）。

两个通道（都是 opt-in，默认关，见 settings.push_*_enabled）：
- toast  ：本机 Windows 系统通知（winotify），零外网依赖，适合「电脑就在面前」。
- wechat ：Server酱 微信推送（需要 SendKey），适合「人不在电脑前」，手机微信直接收。
"""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

# app_id 是 Windows AUMID：必须用 ASCII（winotify 不支持非 ASCII，中文会让
# 通知静默失效——CreateToastNotifier 不报错、通知也不进通知中心）。
TOAST_APP_ID = "CognitiveCopilot"
WECHAT_URL = "https://sctapi.ftqq.com/{sendkey}.send"


def send_toast(title: str, body: str) -> bool:
    """本机系统通知。winotify 未安装 / 非 Windows 时静默降级 False。

    winotify 在函数内延迟 import：缺依赖只代表「这个通道不生效」，
    不该让整个推送模块在 import 阶段就崩。
    """
    try:
        from winotify import Notification
    except Exception as exc:  # noqa: BLE001 - 缺依赖属预期降级，不该打断
        logger.debug("winotify 未安装，跳过桌面通知：%s", exc)
        return False
    try:
        Notification(app_id=TOAST_APP_ID, title=title[:120], msg=body[:400]).show()
        return True
    except Exception as exc:  # noqa: BLE001 - 桌面环境五花八门，失败只降级
        logger.warning("桌面通知发送失败：%s", exc)
        return False


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
            data={"title": title.replace("\n", " ")[:32], "desp": body},
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
