# -*- coding: utf-8 -*-
"""截取当前版本的真实界面图（供汇报 PPT 使用）。

沿用 _shots/shoot.py 的无头 Edge + CDP 方案，改为 8010 端口、输出到本次汇报的
assets 目录，并支持「指定会话」「点一下某按钮」这两类页面。
只读：仅在浏览器里注入本地登录态，不改任何业务数据。
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import time
from uuid import uuid4

import jwt
import requests
from websocket import create_connection

ROOT = r"D:\知识学习智能体"
BASE = "http://127.0.0.1:8010"
USER = "358825dd-4b7e-4bac-91f7-a32522c18b20"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PROFILE = os.path.join(ROOT, "output", "_shots", "_profile_v3")
OUT = os.path.join(ROOT, "output", "认知副驾功能汇报-2026-11-11", "assets")
BOOK = "dc2ecd47-0e7d-49ac-abac-0bfd962166e3"

_DISMISS = (
    "document.querySelectorAll('.notify-pop-close').forEach(function(b){b.click()});'ok'"
)
_SCROLL_UP = (
    "var el=document.getElementById('agent-log');"
    "if(el){el.scrollTop = Math.max(0, el.scrollHeight - el.clientHeight - 300);}'ok'"
)
_SELECT = (
    "(function(){var b=document.getElementById('reader-body');"
    "var w=document.createTreeWalker(b,NodeFilter.SHOW_TEXT),n=w.nextNode();"
    "var k=0;while(n&&(n.nodeValue.trim().length<60||k<8)){if(n.nodeValue.trim().length>=60){k++;}n=w.nextNode();}"
    "if(!n){return 'no-node';}"
    "var r=document.createRange();r.setStart(n,0);r.setEnd(n,Math.min(28,n.nodeValue.length));"
    "var s=window.getSelection();s.removeAllRanges();s.addRange(r);"
    "var rc=r.getBoundingClientRect();"
    "document.dispatchEvent(new MouseEvent('mouseup',{bubbles:true,clientX:rc.left+40,clientY:rc.top}));"
    "return 'ok';})()"
)

# name: (path, w, h, scale, mobile, 会话 id, [截图前 JS], 等待秒数)
PAGES = {
    # 计划 + 资料收集 + 需求确认三类卡片同屏
    "chat_plan": (
        "app.html", 1440, 900, 2, False, "0378d203-bfc7-4448-9fa9-019ae281a3cc",
        [_DISMISS, _SCROLL_UP], 4.0,
    ),
    # 健康诊断卡片
    "chat_l5": (
        "app.html", 1440, 900, 2, False, "ab17622a-5221-49a8-b903-56a98011ba16",
        [_DISMISS], 4.0,
    ),
    "reader_sel": ("reader.html?book=" + BOOK, 1440, 900, 2, False, None, [_SELECT], 3.5),
    "chat_plan3": (
        "app.html", 1440, 900, 2, False, "a1c64b36-0e8a-4fce-bb09-80b4d833158e",
        [_DISMISS], 4.0,
    ),
    "chat_suggest": (
        "app.html", 1440, 900, 2, False, "c58a55e2-b115-4a30-b6f9-2eaed6339922",
        [_DISMISS], 4.0,
    ),
    "chat_plan2": (
        "app.html", 1440, 900, 2, False, "8e3ec7f1-26cb-4b20-b93b-1280f14ff9e4",
        [_DISMISS], 4.0,
    ),
    "knowledge": ("knowledge.html", 1440, 900, 2, False, None, [], 3.0),
    "books": ("books.html", 1440, 900, 2, False, None, [], 3.0),
    "reader": ("reader.html?book=" + BOOK, 1440, 900, 2, False, None, [], 3.5),
    "reading_log": ("reading_log.html", 1440, 900, 2, False, None, [], 3.0),
    "mine_web": (
        "mine.html",
        1440,
        900,
        2,
        False,
        None,
        ['document.querySelector("#mode-tabs button[data-mode=\\"web\\"]").click()'],
        2.0,
    ),
    "conflicts": (
        "conflicts.html",
        1440,
        900,
        2,
        False,
        None,
        ['document.querySelector("#cf-tabs button[data-state=\\"accepted\\"]").click()'],
        2.5,
    ),
    "brief": ("brief.html", 1440, 900, 2, False, None, ['document.getElementById("brief-generate").click()', _DISMISS], 5.0),
    "brief": ("brief.html", 1440, 900, 2, False, None, ['document.getElementById("brief-generate").click()', _DISMISS], 5.0),
    "l4": ("l4.html", 1440, 900, 2, False, None, [], 3.0),
    "l5": ("l5.html", 1440, 900, 2, False, None, [], 3.0),
    "notify": ("notify.html", 1440, 900, 2, False, None, [], 3.0),
    "mobile": ("app.html", 390, 844, 2, True, None, [], 3.5),
}


def secret() -> str:
    for line in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
        s = line.strip()
        if s.startswith("JWT_SECRET="):
            return s.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("未找到 JWT_SECRET")


def make_token() -> str:
    now = int(time.time())
    payload = {
        "sub": USER,
        "typ": "access",
        "ver": 0,
        "iat": now,
        "exp": now + 7200,
        "jti": uuid4().hex,
    }
    return jwt.encode(payload, secret(), algorithm="HS256")


class Page:
    def __init__(self, ws_url: str):
        self.ws = create_connection(ws_url, timeout=60)
        self.i = 0

    def cmd(self, method: str, **params):
        self.i += 1
        self.ws.send(json.dumps({"id": self.i, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.i:
                if "error" in msg:
                    raise RuntimeError("%s: %s" % (method, msg["error"]))
                return msg.get("result", {})

    def eval(self, expr: str):
        r = self.cmd("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        return r.get("result", {}).get("value")

    def viewport(self, w: int, h: int, scale: int, mobile: bool):
        self.cmd(
            "Emulation.setDeviceMetricsOverride",
            width=w, height=h, deviceScaleFactor=scale, mobile=mobile,
        )

    def goto(self, url: str, wait: float = 3.0):
        self.cmd("Page.navigate", url=url)
        for _ in range(80):
            time.sleep(0.25)
            try:
                if self.eval("document.readyState") == "complete":
                    break
            except Exception:
                pass
        time.sleep(wait)

    def shot(self, path: str):
        r = self.cmd("Page.captureScreenshot", format="png", captureBeyondViewport=False)
        with open(path, "wb") as fh:
            fh.write(base64.b64decode(r["data"]))
        return path


def wait_debugger(timeout: float = 30.0) -> str:
    end = time.time() + timeout
    while time.time() < end:
        try:
            r = requests.get("http://127.0.0.1:9222/json/list", timeout=2)
            for t in r.json():
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                    return t["webSocketDebuggerUrl"]
        except Exception:
            pass
        time.sleep(0.4)
    raise SystemExit("CDP 没有就绪")


def main() -> int:
    want = sys.argv[1:] or list(PAGES)
    os.makedirs(OUT, exist_ok=True)
    tok = make_token()

    r = requests.get(BASE + "/api/v1/auth/me", headers={"Authorization": "Bearer " + tok}, timeout=10)
    print("auth/me ->", r.status_code, r.text[:80])
    if r.status_code != 200:
        print("登录态自检失败，停止")
        return 1

    if os.path.isdir(PROFILE):
        shutil.rmtree(PROFILE, ignore_errors=True)

    proc = subprocess.Popen(
        [
            EDGE, "--headless=new", "--disable-gpu", "--no-first-run",
            "--no-default-browser-check", "--disable-extensions", "--hide-scrollbars",
            "--remote-allow-origins=*", "--remote-debugging-port=9222",
            "--user-data-dir=" + PROFILE, "--window-size=1440,900", "about:blank",
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    try:
        p = Page(wait_debugger())
        p.cmd("Page.enable")
        p.cmd("Runtime.enable")
        p.viewport(1440, 900, 2, False)

        p.goto(BASE + "/login.html", 1.0)
        p.eval(
            "localStorage.setItem('cc_access_token', %s);"
            "localStorage.setItem('cc_username', '111');"
            "localStorage.setItem('pwa_install_dismissed', '1');'ok'" % json.dumps(tok)
        )
        print("已注入登录态")

        for name in want:
            if name not in PAGES:
                print("跳过未知页面:", name)
                continue
            path, w, h, scale, mobile, conv, actions, wait = PAGES[name]
            p.viewport(w, h, scale, mobile)
            if conv:
                p.eval("localStorage.setItem('cc_agent_conversation', %s);'ok'" % json.dumps(conv))
            else:
                p.eval("localStorage.removeItem('cc_agent_conversation');'ok'")
            p.goto(BASE + "/" + path, wait)
            for js in actions:
                try:
                    p.eval(js)
                except Exception as exc:
                    print("  动作失败:", js, exc)
                time.sleep(40.0 if path.startswith("brief") else 2.0)
            out = os.path.join(OUT, name + ".png")
            p.shot(out)
            print("截好:", name)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
