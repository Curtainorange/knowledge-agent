# -*- coding: utf-8 -*-
"""按脚本操作真实界面并逐帧录制，叠加字幕与进度条，最后编码成 MP4。

不是 AI 生成视频：画面全部来自本机 8010 上的真实页面（无头 Edge + CDP），
动作（滚动 / 划词 / 点击）由脚本在页面上真实执行。

用法：
    python record_video.py short   # 90 秒精华版
    python record_video.py full    # 3.5 分钟完整版
"""
from __future__ import annotations

import base64
import io
import json
import os
import shutil
import subprocess
import sys
import time
from uuid import uuid4

import jwt
import requests
from PIL import Image, ImageDraw, ImageFont
from websocket import create_connection

ROOT = r"D:\知识学习智能体"
BASE = "http://127.0.0.1:8010"
USER = "358825dd-4b7e-4bac-91f7-a32522c18b20"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PROFILE = os.path.join(ROOT, "output", "_shots", "_profile_rec")
OUTDIR = os.path.join(ROOT, "output", "认知副驾功能汇报-2026-11-11", "video")
BOOK = "dc2ecd47-0e7d-49ac-abac-0bfd962166e3"

VW, VH, SCALE = 1280, 720, 1.25
W, H = int(VW * SCALE), int(VH * SCALE)  # 截图尺寸（1600x900）
OUT_W, OUT_H = 1920, 1080                # 成片尺寸
FPS = 8

FONT = r"C:\Windows\Fonts\msyh.ttc"
FONT_B = r"C:\Windows\Fonts\msyhbd.ttc"


# ---------------------------------------------------------------- 动作
SELECT_JS = (
    "(function(){var b=document.getElementById('reader-body');"
    "var w=document.createTreeWalker(b,NodeFilter.SHOW_TEXT),n=w.nextNode();"
    "while(n&&n.nodeValue.trim().length<60){n=w.nextNode();}"
    "if(!n){return 'no-node';}"
    "var r=document.createRange();r.setStart(n,0);r.setEnd(n,Math.min(26,n.nodeValue.length));"
    "var s=window.getSelection();s.removeAllRanges();s.addRange(r);"
    "var rc=r.getBoundingClientRect();"
    "document.dispatchEvent(new MouseEvent('mouseup',{bubbles:true,clientX:rc.left+40,clientY:rc.top}));"
    "return 'ok';})()"
)


def act(js: str) -> str:
    """把动作串翻译成 JS。"""
    if js == "select":
        return SELECT_JS
    if js.startswith("scroll:"):
        _, target, px = js.split(":")
        sel = "null" if target in ("window", "body") else "document.querySelector('%s')" % target
        return (
            "(function(sel,y,ms){var el=sel||null;"
            "var host=el||document.scrollingElement||document.documentElement;"
            "var start=host.scrollTop,t0=performance.now();"
            "function step(){var k=Math.min(1,(performance.now()-t0)/ms);"
            "var v=start+(y-start)*k;"
            "if(el){el.scrollTop=v;}else{window.scrollTo(0,v);}"
            "if(k<1){requestAnimationFrame(step);}}"
            "requestAnimationFrame(step);return 'ok';})(%s,%s,2600)" % (sel, px)
        )
    if js.startswith("click:"):
        sel = js[6:]
        return (
            "(function(){var el=document.querySelector('%s');"
            "if(el){el.click();return 'clicked';}return 'missing';})()" % sel
        )
    return js


DISMISS = "document.querySelectorAll('.notify-pop-close').forEach(function(b){b.click()});'ok'"


# ---------------------------------------------------------------- 场景
# (type, 参数, 时长秒, 章节, 字幕, [(相对秒, 动作)])
SHORT = [
    ("title", dict(title="认知副驾", sub="功能演示 · 2026-11-11", note="真实界面录制，非演示动画"), 3.0, "", "", []),

    ("page", dict(path="app.html", conv="a1c64b36-0e8a-4fce-bb09-80b4d833158e"), 18.0,
     "① 对话即入口",
     "不用点功能，只说一句要做什么；回来的是可以继续操作的卡片",
     [(1.0, "scroll:#agent-log:0"), (5.0, "scroll:#agent-log:420"), (10.0, "scroll:#agent-log:760")]),

    ("page", dict(path="reader.html?book=" + BOOK), 18.0,
     "② 划词即录入",
     "读到有用的段落，选中就存；也可以把原文和自己的想法一起存成一条",
     [(2.0, "select"), (8.0, "click:#selection-pop button[data-act=\"excerpt\"]")]),

    ("page", dict(path="conflicts.html"), 18.0,
     "③ 冲突检测",
     "两条主张对立时，把两侧原句摆出来，并给出可验证的判据",
     [(1.0, "click:#cf-tabs button[data-state=\"accepted\"]"), (7.0, "scroll:window:220")]),

    ("page", dict(path="l5.html"), 18.0,
     "④ 健康诊断",
     "不只报数字：归因、建议、完整的推理链，都能被追问",
     [(4.0, "scroll:window:260"), (10.0, "scroll:window:520")]),

    ("page", dict(path="notify.html"), 12.0,
     "⑤ 送到眼前",
     "站内弹窗与手机微信两条通道，都可以自己关掉",
     [(4.0, "scroll:window:300")]),

    ("title", dict(title="把收藏的碎片，变成可调用的认知", sub="找得到 · 看得出矛盾 · 问得出问题 · 改得动计划", note="谢谢"), 4.0, "", "", []),
]

FULL = [
    ("title", dict(title="认知副驾", sub="功能演示 · 2026-11-11", note="真实界面录制，非演示动画"), 3.0, "", "", []),

    ("page", dict(path="app.html", conv="a1c64b36-0e8a-4fce-bb09-80b4d833158e"), 20.0,
     "① 对话即入口", "不用点功能，只说一句要做什么；回来的是可以继续操作的卡片",
     [(1.0, "scroll:#agent-log:0"), (6.0, "scroll:#agent-log:420"), (12.0, "scroll:#agent-log:780")]),

    ("page", dict(path="knowledge.html"), 18.0,
     "② 知识库", "24 条知识全部已向量化；来源、标签、检索失败率一眼可见",
     [(4.0, "scroll:window:240")]),

    ("page", dict(path="reader.html?book=" + BOOK), 20.0,
     "③ 阅读器", "选中文字即可存摘录，或写下自己的想法；已录过的段落会自动高亮",
     [(2.0, "select"), (9.0, "click:#selection-pop button[data-act=\"excerpt\"]"), (14.0, "click:#btn-notes")]),

    ("page", dict(path="books.html"), 16.0,
     "④ 书架", "txt / epub / pdf 自动分章；通读、聊书、推荐、对比两本书都在这一页",
     [(5.0, "scroll:window:200")]),

    ("page", dict(path="app.html", conv="0378d203-bfc7-4448-9fa9-019ae281a3cc"), 20.0,
     "⑤ 资料收集与计划", "拆计划时顺带把学习材料找好，勾选之后才入库",
     [(2.0, "scroll:#agent-log:220"), (8.0, "scroll:#agent-log:640"), (14.0, "scroll:#agent-log:980")]),

    ("page", dict(path="app.html", conv="c58a55e2-b115-4a30-b6f9-2eaed6339922"), 18.0,
     "⑥ 按依据给建议", "每条建议都带出处；依据不够时，它宁可不给",
     [(2.0, "scroll:#agent-log:200"), (9.0, "scroll:#agent-log:620")]),

    ("page", dict(path="mine.html"), 14.0,
     "⑦ L1 挖掘", "只记得情节或作者，也能把书找回来；结果不入库，留不留由你决定",
     [(2.0, "click:#mode-tabs button[data-mode=\"web\"]")]),

    ("page", dict(path="conflicts.html"), 20.0,
     "⑧ L2 冲突检测", "把知识体系里的「内战」摆到台面上：两侧原句、依据、建议、为什么冲突",
     [(1.0, "click:#cf-tabs button[data-state=\"accepted\"]"), (6.0, "scroll:window:200"),
      (12.0, "scroll:window:420")]),

    ("image", dict(path="brief.png"), 18.0,
     "⑨ L3 认知简报", "先看清主题结构，再问出「该问但没问」的问题", []),

    ("page", dict(path="l4.html"), 18.0,
     "⑩ L4 路径修正", "模型排任务、代码管约束；排期参数在页面上就能改",
     [(5.0, "scroll:window:300"), (11.0, "scroll:window:620")]),

    ("page", dict(path="l5.html"), 18.0,
     "⑪ L5 健康诊断", "归因、建议与推理链；采纳与否都由你决定",
     [(4.0, "scroll:window:280"), (10.0, "scroll:window:540")]),

    ("page", dict(path="notify.html"), 14.0,
     "⑫ 主动推送", "抑制先于发送：免打扰、去重、忽略多次后收敛",
     [(4.0, "scroll:window:320")]),

    ("image", dict(path="fig_grounding.png"), 12.0,
     "技术一分钟", "建议必须有出处：只有能命中原文、能跳回定位的内容才算依据", []),

    ("title", dict(title="把收藏的碎片，变成可调用的认知", sub="找得到 · 看得出矛盾 · 问得出问题 · 改得动计划", note="谢谢"), 4.0, "", "", []),
]


# ---------------------------------------------------------------- CDP
def secret() -> str:
    for line in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
        s = line.strip()
        if s.startswith("JWT_SECRET="):
            return s.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("未找到 JWT_SECRET")


def make_token() -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": USER, "typ": "access", "ver": 0, "iat": now, "exp": now + 7200, "jti": uuid4().hex},
        secret(), algorithm="HS256",
    )


class Page:
    def __init__(self, ws_url: str):
        self.ws = create_connection(ws_url, timeout=90)
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
        try:
            r = self.cmd("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
            return r.get("result", {}).get("value")
        except Exception:
            return None

    def viewport(self, w: int, h: int, scale: float, mobile: bool = False):
        self.cmd("Emulation.setDeviceMetricsOverride",
                 width=w, height=h, deviceScaleFactor=scale, mobile=mobile)

    def goto(self, url: str, wait: float = 2.5):
        self.cmd("Page.navigate", url=url)
        for _ in range(80):
            time.sleep(0.2)
            if self.eval("document.readyState") == "complete":
                break
        time.sleep(wait)

    def grab(self) -> bytes:
        r = self.cmd("Page.captureScreenshot", format="jpeg", quality=70, fromSurface=True, captureBeyondViewport=False)
        return base64.b64decode(r["data"])


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
    raise SystemExit("CDP 没有就绪")


# ---------------------------------------------------------------- 合成
def font(size: int, bold: bool = False):
    return ImageFont.truetype(FONT_B if bold else FONT, size)


def draw_title(title: str, sub: str, note: str, progress: float) -> Image.Image:
    W, H = OUT_W, OUT_H
    img = Image.new("RGB", (W, H), "#FFFFFF")
    d = ImageDraw.Draw(img)
    d.rectangle([88, 380, 96, 700], fill="#4F46E5")
    d.text((140, 386), title, font=font(92, True), fill="#1F2937")
    d.text((146, 512), sub, font=font(34), fill="#475569")
    d.line([140, 600, 900, 600], fill="#E2E8F0", width=2)
    d.text((146, 626), note, font=font(26), fill="#94A3B8")
    d.text((146, 980), "认知副驾 · 个人知识学习智能体", font=font(24), fill="#94A3B8")
    d.rectangle([0, H - 6, int(W * progress), H], fill="#6366F1")
    return img


def draw_image_frame(path: str, caption: str, chapter: str, progress: float) -> Image.Image:
    W, H = OUT_W, OUT_H
    bg = Image.new("RGB", (W, H), "#FFFFFF")
    src = Image.open(path).convert("RGB")
    ratio = min((W - 160) / src.width, (H - 220) / src.height)
    src = src.resize((int(src.width * ratio), int(src.height * ratio)), Image.LANCZOS)
    bg.paste(src, ((W - src.width) // 2, (H - 150 - src.height) // 2 + 40))
    return overlay(bg, caption, chapter, progress)


_BAR_CACHE: dict[tuple[str, str], Image.Image] = {}


def _bar(caption: str, chapter: str) -> Image.Image:
    """字幕底条只渲染一次，逐帧复用。"""
    W, H = OUT_W, OUT_H
    key = (caption, chapter)
    if key not in _BAR_CACHE:
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        d.rectangle([0, 946, W, H], fill=(15, 23, 42, 214))
        if chapter:
            d.text((64, 962), chapter, font=font(26), fill=(165, 180, 252, 255))
        if caption:
            d.text((64, 998), caption, font=font(34, True), fill=(255, 255, 255, 255))
        _BAR_CACHE[key] = layer
    return _BAR_CACHE[key]


def overlay(img: Image.Image, caption: str, chapter: str, progress: float) -> Image.Image:
    """底部字幕条 + 进度条（只合成底部条带，避免整图 alpha 合成）。"""
    W, H = OUT_W, OUT_H
    top = 946
    band = img.crop((0, top, W, H)).convert("RGBA")
    bar = _bar(caption, chapter).crop((0, top, W, H))
    merged = Image.alpha_composite(band, bar)
    d = ImageDraw.Draw(merged)
    d.rectangle([0, H - top - 6, int(W * progress), H - top], fill=(99, 102, 241, 255))
    out = img.copy()
    out.paste(merged.convert("RGB"), (0, top))
    return out


def fade(img: Image.Image, factor: float) -> Image.Image:
    """factor=0 全黑，1 原图。"""
    if factor >= 0.999:
        return img
    return Image.blend(Image.new("RGB", img.size, "#0B1020"), img, max(0.0, factor))


def encode(frames_dir: str, out_path: str, nframes: int, seconds: float):
    """按「实际抓到的帧数 / 计划时长」还原真实节奏。

    抓帧速率受截图耗时限制（约 5-6 fps），若把输入帧率写死成目标值，
    整段视频会被压缩成原来的几分之一——动作会快到看不清。
    """
    import imageio_ffmpeg
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    in_fps = max(1.0, nframes / max(seconds, 1.0))
    cmd = [
        exe, "-y", "-framerate", "%.4f" % in_fps, "-i", os.path.join(frames_dir, "%05d.jpg"),
        "-vf", "fps=24,scale=in_range=full:out_range=limited,format=yuv420p",
        "-c:v", "libx264", "-crf", "20", "-color_range", "tv",
        "-preset", "medium", "-movflags", "+faststart", out_path,
    ]
    print("编码中…")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return out_path


# ---------------------------------------------------------------- 主流程
def main() -> int:
    which = (sys.argv[1] if len(sys.argv) > 1 else "short").lower()
    scenes = SHORT if which == "short" else FULL
    tag = "short" if which == "short" else "full"
    start = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    frames_dir = os.path.join(OUTDIR, "_frames_" + tag)
    if start == 0 and os.path.isdir(frames_dir):
        shutil.rmtree(frames_dir, ignore_errors=True)
    os.makedirs(frames_dir, exist_ok=True)
    os.makedirs(OUTDIR, exist_ok=True)

    total = sum(s[2] for s in scenes)
    tok = make_token()
    if requests.get(BASE + "/api/v1/auth/me",
                    headers={"Authorization": "Bearer " + tok}, timeout=10).status_code != 200:
        print("登录态自检失败")
        return 1

    if os.path.isdir(PROFILE):
        shutil.rmtree(PROFILE, ignore_errors=True)
    proc = subprocess.Popen(
        [EDGE, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
         "--disable-extensions", "--hide-scrollbars", "--remote-allow-origins=*",
         "--remote-debugging-port=9222", "--user-data-dir=" + PROFILE,
         "--window-size=%d,%d" % (VW, VH), "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    idx = len([f for f in os.listdir(frames_dir) if f.endswith(".jpg")])
    t0 = time.time()
    try:
        p = Page(wait_debugger())
        p.cmd("Page.enable")
        p.cmd("Runtime.enable")
        p.viewport(VW, VH, SCALE)

        p.goto(BASE + "/login.html", 0.8)
        p.eval("localStorage.setItem('cc_access_token', %s);"
               "localStorage.setItem('cc_username','111');"
               "localStorage.setItem('pwa_install_dismissed','1');'ok'" % json.dumps(tok))
        print("已注入登录态，预计总时长 %ds" % total)

        for si, (kind, params, dur, chapter, caption, events) in enumerate(scenes):
            if si < start:
                continue
            if kind == "page":
                if params.get("conv"):
                    p.eval("localStorage.setItem('cc_agent_conversation', %s);'ok'"
                           % json.dumps(params["conv"]))
                else:
                    p.eval("localStorage.removeItem('cc_agent_conversation');'ok'")
                p.goto(BASE + "/" + params["path"], 2.5)
                p.eval(DISMISS)
            elif kind == "image":
                pass

            pending = list(events)
            t_scene = time.time()
            last_dismiss = t_scene
            offset = sum(s[2] for s in scenes[:si])
            while True:
                elapsed = time.time() - t_scene
                if elapsed >= dur:
                    break
                while pending and elapsed >= pending[0][0]:
                    _, js = pending.pop(0)
                    if kind == "page":
                        p.eval(act(js))
                if kind == "page" and time.time() - last_dismiss > 2.5:
                    p.eval(DISMISS)
                    last_dismiss = time.time()

                progress = min(1.0, (offset + elapsed) / total)
                frame_start = time.time()
                if kind == "page":
                    raw = p.grab()
                    img = Image.open(io.BytesIO(raw)).convert("RGB")
                    if img.size != (OUT_W, OUT_H):
                        img = img.resize((OUT_W, OUT_H), Image.BILINEAR)
                    frame = overlay(img, caption, chapter, progress)
                elif kind == "image":
                    frame = draw_image_frame(os.path.join(OUTDIR, "..", "assets", params["path"]),
                                             caption, chapter, progress)
                else:
                    frame = draw_title(params["title"], params["sub"], params["note"], progress)

                # 场景切换：首 0.35s 淡入、末 0.35s 淡出
                f = 1.0
                if elapsed < 0.35:
                    f = elapsed / 0.35
                elif dur - elapsed < 0.35:
                    f = (dur - elapsed) / 0.35
                frame = fade(frame, f)

                idx += 1
                frame.save(os.path.join(frames_dir, "%05d.jpg" % idx), quality=85, subsampling=2)
                time.sleep(max(0.0, 1.0 / FPS - (time.time() - frame_start)))
            print("  场景完成：%s（%ds，累计 %d 帧）" % (chapter or "标题卡", dur, idx))

        out = os.path.join(OUTDIR, "认知副驾功能演示-%s.mp4" % ("90秒" if tag == "short" else "完整版"))
        encode(frames_dir, out, idx, total)
        print("完成：%s（%d 帧，%.0fs 素材，耗时 %.0fs）"
              % (out, idx, total, time.time() - t0))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
