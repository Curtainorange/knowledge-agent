# -*- coding: utf-8 -*-
"""真实操作驱动：有头全屏 Edge + CDP，在真实页面上逐字打字、点击、滚动、等结果。

与旧版（无头逐帧截图）的区别：
- 画面由 ffmpeg 采集桌面得到，30fps，不经过截图接口，不会卡成 5fps；
- 所有操作都是真的：真打字、真点按钮、真等模型返回，而不是预先存好的页面。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from uuid import uuid4

import jwt
import requests
from websocket import create_connection

ROOT = r"D:\知识学习智能体"
BASE = "http://127.0.0.1:8010"
USER = "358825dd-4b7e-4bac-91f7-a32522c18b20"
DISPLAY_NAME = "周宇航"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PROFILE = os.path.join(ROOT, "output", "_shots", "_profile_live")

# 每次进页面都重装：关掉「装到手机桌面」提示条、抑制通知弹窗，
# 并把右上角用户名固定成演示用的名字（页面加载后会拿接口返回的 username 覆盖，
# 所以必须用 interval 持续纠正，只改本地显示、不动数据库）。
BOOT_JS = """
(function(){
  localStorage.setItem('pwa_install_dismissed','1');
  window.confirm = function(){ return true; };   // 删除类操作的原生确认框，录制时直接放行
  var NAME = %s;
  if (window.__nameFix) { clearInterval(window.__nameFix); }
  window.__nameFix = setInterval(function(){
    var e = document.getElementById('who-name');
    if (e && e.textContent !== NAME) { e.textContent = NAME; }
  }, 300);
  if (!window.__popFix) {
    window.__popFix = setInterval(function(){
      document.querySelectorAll('.notify-pop-close').forEach(function(b){ b.click(); });
    }, 1200);
  }
  return 'ok';
})()
"""


def secret() -> str:
    for line in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
        s = line.strip()
        if s.startswith("JWT_SECRET="):
            return s.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("未找到 JWT_SECRET")


def make_token(hours: int = 6) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": USER, "typ": "access", "ver": 0, "iat": now,
         "exp": now + hours * 3600, "jti": uuid4().hex},
        secret(), algorithm="HS256",
    )


class Driver:
    def __init__(self, port: int = 9224, width: int = 1920, height: int = 1080):
        self.port = port
        self.t0 = time.time()
        self.marks: list[tuple[float, str, str]] = []
        self.proc = None
        self.ws = None
        self.i = 0
        self.width, self.height = width, height

    # ---------------------------------------------------------------- 启停
    def start(self, headless: bool = False):
        if os.path.isdir(PROFILE):
            shutil.rmtree(PROFILE, ignore_errors=True)
        mode = (["--headless=new", "--disable-gpu"] if headless else ["--kiosk"])
        self.proc = subprocess.Popen(
            [EDGE] + mode + ["--no-first-run", "--no-default-browser-check",
             "--disable-extensions", "--hide-scrollbars", "--remote-allow-origins=*",
             "--remote-debugging-port=%d" % self.port, "--user-data-dir=" + PROFILE,
             "--window-size=%d,%d" % (self.width, self.height),
             "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        url = self._wait_tab()
        self.ws = create_connection(url, timeout=120)
        self.cmd("Page.enable")
        self.cmd("Runtime.enable")
        if headless:
            self.cmd("Emulation.setDeviceMetricsOverride", width=self.width,
                     height=self.height, deviceScaleFactor=1, mobile=False)
        tok = make_token()
        self.goto("login.html", 1.0)
        self.eval("localStorage.setItem('cc_access_token', %s);"
                  "localStorage.setItem('cc_username', %s);'ok'"
                  % (json.dumps(tok), json.dumps(DISPLAY_NAME, ensure_ascii=False)))
        time.sleep(0.6)
        self.t0 = time.time()
        return self

    def _wait_tab(self, timeout: float = 40.0) -> str:
        end = time.time() + timeout
        while time.time() < end:
            try:
                for t in requests.get("http://127.0.0.1:%d/json/list" % self.port,
                                      timeout=2).json():
                    if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                        return t["webSocketDebuggerUrl"]
            except Exception:
                pass
            time.sleep(0.3)
        raise SystemExit("CDP 未就绪")

    def stop(self):
        try:
            self.ws.close()
        except Exception:
            pass
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()

    # ---------------------------------------------------------------- CDP
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
        try:
            r = self.cmd("Runtime.evaluate", expression=expr, returnByValue=True,
                         awaitPromise=True)
            return r.get("result", {}).get("value")
        except Exception:
            return None

    # ---------------------------------------------------------------- 时间轴
    def mark(self, chapter: str, caption: str):
        """切换字幕（后期按时间轴烧进画面）。时间戳用绝对时间，便于与录屏对齐。"""
        if self.marks and self.marks[-1][1:] == (chapter, caption):
            return
        self.marks.append((time.time(), chapter, caption))
        print("  [%6.1fs] %s — %s" % (time.time() - self.t0, chapter, caption))

    # ---------------------------------------------------------------- 动作
    def goto(self, path: str, wait: float = 1.6):
        self.cmd("Page.navigate", url=BASE + "/" + path)
        for _ in range(120):
            time.sleep(0.15)
            if self.eval("document.readyState") == "complete":
                break
        time.sleep(wait)
        self.eval(BOOT_JS % json.dumps(DISPLAY_NAME, ensure_ascii=False))

    def set_conv(self, conv_id: str | None):
        if conv_id:
            self.eval("localStorage.setItem('cc_agent_conversation', %s);'ok'"
                      % json.dumps(conv_id))
        else:
            self.eval("localStorage.removeItem('cc_agent_conversation');'ok'")

    def click(self, sel: str, wait: float = 0.35) -> str:
        r = self.eval(
            "(function(){var el=document.querySelector(%s);"
            "if(!el){return 'missing';}"
            "el.scrollIntoView({block:'center'});el.click();return 'ok';})()"
            % json.dumps(sel))
        time.sleep(wait)
        return r or "null"

    def type_text(self, sel: str, text: str, cps: float = 22.0, tail: float = 0.4):
        """逐字输入，模拟真人打字速度（录屏里能看到字一个个出来）。"""
        self.eval("(function(){var el=document.querySelector(%s);if(el){el.focus();"
                  "el.value='';el.dispatchEvent(new Event('input',{bubbles:true}));}})()"
                  % json.dumps(sel))
        delay = 1.0 / max(cps, 1.0)
        step = 1
        for i in range(1, len(text) + 1, step):
            self.eval("(function(){var el=document.querySelector(%s);"
                      "if(el){el.value=%s;el.dispatchEvent(new Event('input',{bubbles:true}));}})()"
                      % (json.dumps(sel), json.dumps(text[:i], ensure_ascii=False)))
            time.sleep(delay)
        time.sleep(tail)

    def scroll(self, sel: str, y, ms: int = 700):
        """平滑滚动：y 为像素，也可以是 'end'。"""
        js = (
            "(function(sel,y,ms){var el=sel?document.querySelector(sel):null;"
            "var host=el||document.scrollingElement||document.documentElement;"
            "var target=(y==='end')?(host.scrollHeight):y;"
            "var start=host.scrollTop,t0=performance.now();"
            "function step(){var k=Math.min(1,(performance.now()-t0)/ms);"
            "var v=start+(target-start)*k;"
            "if(el){el.scrollTop=v;}else{window.scrollTo(0,v);}"
            "if(k<1){requestAnimationFrame(step);}}"
            "requestAnimationFrame(step);return 'ok';})(%s,%s,%d)"
            % (json.dumps(sel), json.dumps(y) if isinstance(y, str) else y, ms))
        self.eval(js)
        time.sleep(ms / 1000.0 + 0.25)

    def wheel(self, sel: str, dy: int, ms: int = 600):
        """以「滚轮」方式滚动指定容器（对内部滚动区更自然）。"""
        self.scroll(sel, "+=0", 1)  # noqa: 占位，保持接口一致性
        cur = self.eval("(function(){var el=%s;var h=el||document.scrollingElement;"
                        "return h?h.scrollTop:0;})()" % (json.dumps(sel) if sel else "null"))
        self.scroll(sel, int(cur or 0) + dy, ms)

    def select_paragraph(self, nchars: int = 26, body_sel: str = "#reader-body"):
        """在正文里选中一段**当前视口内可见**的文字，并弹出划词浮层。

        参数顺序按调用习惯定：先给字数。之前 body_sel 在前，剧本里
        `select_paragraph(26)` 把 26 当成了选择器，querySelector 直接报错，
        表现为「划词没反应」。
        """
        js = (
            "(function(){var b=document.querySelector(%s);"
            "if(!b){return 'no-body';}"
            "var w=document.createTreeWalker(b,NodeFilter.SHOW_TEXT),n,pick=null;"
            "while((n=w.nextNode())){"
            "if(n.nodeValue.trim().length<18){continue;}"
            "var box=n.parentElement.getBoundingClientRect();"
            "if(box.top>=150&&box.top<=820&&box.left>=0&&box.right<=window.innerWidth){"
            "pick=n;break;}}"
            "if(!pick){return 'no-visible';}"
            "var r=document.createRange();r.setStart(pick,0);"
            "r.setEnd(pick,Math.min(%d,pick.nodeValue.length));"
            "var s=window.getSelection();s.removeAllRanges();s.addRange(r);"
            "var rc=r.getBoundingClientRect();"
            "['mousedown','mouseup'].forEach(function(t){"
            "document.dispatchEvent(new MouseEvent(t,{bubbles:true,"
            "clientX:rc.left+30,clientY:rc.top+6}));});"
            "return 'ok@'+Math.round(rc.top);})()" % (json.dumps(body_sel), nchars))
        r = self.eval(js)
        print("   划词:", r)
        time.sleep(0.5)

    # ---------------------------------------------------------------- 等待
    def wait_text(self, sel: str, expect: str, timeout: float = 60.0) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            t = self.eval("(function(){var e=document.querySelector(%s);"
                          "return e?e.textContent:'';})()" % json.dumps(sel)) or ""
            if expect in t:
                return True
            time.sleep(0.3)
        return False

    def wait_reply(self, log_sel: str = "#agent-log", timeout: float = 240.0,
                   quiet: float = 2.4) -> bool:
        """等对话回合结束：内容比发送前变长、无 pending 卡、且稳定一段时间。

        只判「无 pending 卡 + 稳定」会在请求刚发出、卡片还没渲染时就成立，
        结果是拍到一半的「处理中…」。所以必须要求内容确实变长。
        """
        base = self.eval("(function(){var l=document.querySelector(%s);"
                         "return l?l.textContent.length:0;})()" % json.dumps(log_sel)) or 0
        end = time.time() + timeout
        last_len, last_change = -1, time.time()
        started = time.time()
        while time.time() < end:
            snap = self.eval(
                "(function(){var l=document.querySelector(%s);if(!l){return '-1|0';}"
                "return [l.textContent.length,"
                "l.querySelectorAll('.pending-card').length].join('|');})()"
                % json.dumps(log_sel)) or "-1|0"
            try:
                ln, pend = [int(x) for x in snap.split("|")]
            except Exception:
                ln, pend = -1, 1
            if ln != last_len:
                last_len, last_change = ln, time.time()
            if (pend == 0 and ln > base + 15
                    and time.time() - last_change > quiet
                    and time.time() - started > 2.0):
                return True
            time.sleep(0.25)
        return False

    def wait_js(self, cond: str, timeout: float = 120.0) -> bool:
        """等某个 JS 条件为真（用于"结果真的出来了"这种判断）。"""
        end = time.time() + timeout
        while time.time() < end:
            if self.eval(cond):
                return True
            time.sleep(0.3)
        return False

    def wait_any(self, sels: list[str], timeout: float = 180.0) -> str | None:
        """等其中任意一个选择器出现内容，返回命中的那个。"""
        end = time.time() + timeout
        while time.time() < end:
            for s in sels:
                v = self.eval("(function(){var e=document.querySelector(%s);"
                              "return e?(e.textContent||'').trim():'';})()" % json.dumps(s))
                if v:
                    return s
            time.sleep(0.4)
        return None

    def shot(self, path: str):
        import base64
        from io import BytesIO

        from PIL import Image
        r = self.cmd("Page.captureScreenshot", format="png")
        Image.open(BytesIO(base64.b64decode(r["data"]))).save(path)

    def grab(self, quality: int = 68) -> bytes:
        """抓一帧 JPEG（fromSurface=True 是这台机器上最快的组合）。"""
        import base64
        r = self.cmd("Page.captureScreenshot", format="jpeg", quality=quality,
                     fromSurface=True, captureBeyondViewport=False)
        return base64.b64decode(r["data"])
