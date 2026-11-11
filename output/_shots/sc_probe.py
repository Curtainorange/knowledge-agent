# -*- coding: utf-8 -*-
"""实时画面通道（Page.startScreencast）的 CDP 封装 + 10 秒吞吐测试。

单独成文件是为了先验证「这条通道到底能给到多少帧」，再决定要不要重录。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from uuid import uuid4

import jwt
import requests
from websocket import create_connection

ROOT = r"D:\知识学习智能体"
BASE = "http://127.0.0.1:8010"
USER = "358825dd-4b7e-4bac-91f7-a32522c18b20"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PROFILE = os.path.join(ROOT, "output", "_shots", "_profile_sc")
VW, VH, SCALE = 1280, 720, 1.5


def secret() -> str:
    for line in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
        s = line.strip()
        if s.startswith("JWT_SECRET="):
            return s.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("no secret")


def token() -> str:
    now = int(time.time())
    return jwt.encode({"sub": USER, "typ": "access", "ver": 0, "iat": now,
                       "exp": now + 7200, "jti": uuid4().hex}, secret(), algorithm="HS256")


class Live:
    """单线程收包：response 按 id 唤醒等待者，screencastFrame 存最新帧并回 ack。"""

    def __init__(self, ws_url: str):
        self.ws = create_connection(ws_url, timeout=30, max_size=None)
        self._id = 0
        self._res: dict[int, dict] = {}
        self._cv = threading.Condition()
        self.frame: bytes | None = None
        self.seq = 0
        self.alive = True
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        while self.alive:
            try:
                msg = json.loads(self.ws.recv())
            except Exception:
                self.alive = False
                return
            if "id" in msg:
                with self._cv:
                    self._res[msg["id"]] = msg
                    self._cv.notify_all()
                continue
            if msg.get("method") == "Page.screencastFrame":
                p = msg["params"]
                self.frame = p["data"]
                self.seq += 1
                self._fire("Page.screencastFrameAck", {"sessionId": p["sessionId"]})

    def _fire(self, method: str, params: dict):
        self._id += 1
        try:
            self.ws.send(json.dumps({"id": self._id, "method": method, "params": params}))
        except Exception:
            pass

    def cmd(self, method: str, timeout: float = 30, **params):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        end = time.time() + timeout
        with self._cv:
            while mid not in self._res:
                left = end - time.time()
                if left <= 0:
                    raise RuntimeError("timeout: " + method)
                self._cv.wait(left)
            msg = self._res.pop(mid)
        if "error" in msg:
            raise RuntimeError("%s: %s" % (method, msg["error"]))
        return msg.get("result", {})

    def eval(self, expr: str):
        try:
            r = self.cmd("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
            return r.get("result", {}).get("value")
        except Exception:
            return None


def wait_debugger(timeout: float = 30.0) -> str:
    end = time.time() + timeout
    while time.time() < end:
        try:
            for t in requests.get("http://127.0.0.1:9222/json/list", timeout=2).json():
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                    return t["webSocketDebuggerUrl"]
        except Exception:
            pass
        time.sleep(0.4)
    raise SystemExit("CDP 未就绪")


def main() -> int:
    tok = token()
    if os.path.isdir(PROFILE):
        shutil.rmtree(PROFILE, ignore_errors=True)
    proc = subprocess.Popen(
        [EDGE, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
         "--disable-extensions", "--hide-scrollbars", "--remote-allow-origins=*",
         "--remote-debugging-port=9222", "--user-data-dir=" + PROFILE,
         "--window-size=%d,%d" % (VW, VH), "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        p = Live(wait_debugger())
        p.cmd("Page.enable")
        p.cmd("Runtime.enable")
        p.cmd("Emulation.setDeviceMetricsOverride", width=VW, height=VH,
              deviceScaleFactor=SCALE, mobile=False)
        p.cmd("Page.navigate", url=BASE + "/login.html")
        time.sleep(1.2)
        p.eval("localStorage.setItem('cc_access_token', %s);"
               "localStorage.setItem('cc_username','111');"
               "localStorage.setItem('cc_pwa_dismissed','1');'ok'" % json.dumps(tok))
        p.cmd("Page.navigate", url=BASE + "/app.html")
        time.sleep(3.0)

        p.cmd("Page.startScreencast", format="jpeg", quality=85,
              maxWidth=1920, maxHeight=1080, everyNthFrame=1)
        time.sleep(0.5)

        t0 = time.time()
        seq0 = p.seq
        # 边滚边测：模拟真实的滚动动作
        for i in range(10):
            p.eval("window.scrollTo({top:%d,behavior:'smooth'});'ok'" % (i * 60))
            p.eval("(function(){var el=document.getElementById('agent-log');"
                   "if(el)el.scrollTo({top:%d,behavior:'smooth'});return 'ok';})()" % (i * 50))
            time.sleep(0.25)
        dur = time.time() - t0
        got = p.seq - seq0
        print("screencast: %.1fs 收到 %d 帧 → %.1f fps；最新帧 %s 字节"
              % (dur, got, got / dur, len(p.frame or b"")))
        p.cmd("Page.stopScreencast")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
