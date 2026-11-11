"""推送通道发送器：把 PushService 生成的提醒送到「站内」之外的出口。

延续 ADR-14「推送是旁路」：每个 sender 都尽力而为——失败只记日志、返回 False，
绝不抛异常打断调用方；也不写库、不碰 PushJob 状态机（PushJob 永远是站内通知的权威载体）。

两个通道（都是 opt-in，默认关，见 settings.push_*_enabled）：
- toast  ：本机 Windows 系统通知（ToastGeneric 模板），零外网依赖。
- wechat ：Server酱 微信推送（需要 SendKey），手机微信直接收。

桌面通知为什么不用 winotify：winotify 有三重坑——app_id 不支持非 ASCII（中文静默
失效）、Popen 把 stdout/stderr 全丢（返回 True 也不代表弹出来）、硬编码的
ToastImageAndText02 模板在这台 Win11 上会被静默丢弃（LoadXml 不报错、通知不进通知中心，
11-11 实测）。所以这里自写 PowerShell 脚本 + ToastGeneric 模板，并把内容 base64 传进
脚本（避免任何转义问题）、写临时 .ps1 用 -File 执行（避免 -Command 多行传参坑）、
真正检查 returncode。
"""
from __future__ import annotations

import base64
import logging
import os
import subprocess
import tempfile

import httpx

logger = logging.getLogger(__name__)

# app_id 是 Windows AUMID：必须用 ASCII（中文会让通知静默失效）。
TOAST_APP_ID = "CognitiveCopilot"
WECHAT_URL = "https://sctapi.ftqq.com/{sendkey}.send"

# ToastGeneric 模板（Win10/11 通用）。title/body 以 base64(UTF-8) 注入，脚本里解码 +
# XML 转义后进 toast——标题正文的任意字符（中文、引号、换行）都不会破坏脚本或 XML。
_PS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$title = [Security.SecurityElement]::Escape([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{title_b64}')))
$body = [Security.SecurityElement]::Escape([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{body_b64}')))
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml("<toast duration='short'><visual><binding template='ToastGeneric'><text id='1'>$title</text><text id='2'>$body</text></binding></visual></toast>")
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
$toast.Tag = '{title_b64}'
$toast.Group = '{app_id}'
$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{app_id}')
$notifier.Show($toast)
"""


def send_toast(title: str, body: str) -> bool:
    """本机 Windows 系统通知（ToastGeneric）。任何失败只记日志、返回 False。"""
    title = (title or "")[:120]
    body = (body or "")[:400]
    script = _PS_SCRIPT.format(
        title_b64=base64.b64encode(title.encode("utf-8")).decode("ascii"),
        body_b64=base64.b64encode(body.encode("utf-8")).decode("ascii"),
        app_id=TOAST_APP_ID,
    )

    ps1 = None
    try:
        fd, ps1 = tempfile.mkstemp(suffix=".ps1", prefix="cc_toast_")
        with os.fdopen(fd, "w", encoding="utf-8-sig") as f:
            f.write(script)
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1],
            capture_output=True,
            timeout=20,
        )
        if proc.returncode != 0:
            logger.warning(
                "桌面通知失败 rc=%s err=%s",
                proc.returncode,
                proc.stderr.decode("utf-8", "ignore")[:200],
            )
            return False
        return True
    except Exception as exc:  # noqa: BLE001 - 旁路，失败只降级
        logger.warning("桌面通知发送失败：%s", exc)
        return False
    finally:
        if ps1:
            try:
                os.unlink(ps1)
            except OSError:
                pass


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
