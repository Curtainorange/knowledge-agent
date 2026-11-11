# -*- coding: utf-8 -*-
"""用帧步进驱动 run_live 里的剧本，输出 30fps 成片。

用法：
    python film_run.py s01 s02 ...
    python film_run.py all
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import film  # noqa: E402
import run_live as RL  # noqa: E402
from film import FPS, FrameRecorder  # noqa: E402

OUTDIR = RL.OUTDIR
SEG_ORDER = list(RL.SEGMENTS)


def ease(k: float) -> float:
    """easeInOutQuad：起步和收尾都慢，中间快，滚动看起来更像真人。"""
    return 2 * k * k if k < 0.5 else 1 - pow(-2 * k + 2, 2) / 2


class ScreenTime:
    """把剧本里的 time.sleep(秒) 换成「写这么多帧」，时间由帧数决定。"""

    def __init__(self, d: "FilmDriver"):
        self.d = d

    def sleep(self, seconds: float):
        self.d.hold(seconds)

    def __getattr__(self, name):
        return getattr(time, name)


class FilmDriver(RL.Driver):
    """动作接口与实时版一致，但每个动作都会产出对应数量的帧。"""

    def __init__(self):
        super().__init__()
        self.rec: FrameRecorder | None = None
        self.seg = (1, len(SEG_ORDER))

    # ---------------------------------------------------------------- 基础
    def start(self):
        super().start(headless=True)
        return self

    def snapshot(self, hold: int = 1):
        if not (self.rec and self.rec.started):
            return
        idx = "%02d / %02d" % self.seg
        self.rec.push(self.grab(), max(1, int(hold)), idx)

    def hold(self, seconds: float):
        self.snapshot(max(1, int(round(seconds * FPS))))

    def mark(self, chapter: str, caption: str):
        if self.rec:
            self.rec.mark(chapter, caption)

    # ---------------------------------------------------------------- 页面
    def goto(self, path: str, wait: float = 1.6):
        super().goto(path, min(wait, 0.5))
        self.hold(0.5)

    # ---------------------------------------------------------------- 动作
    def click(self, sel: str, wait: float = 0.35) -> str:
        self.snapshot(2)
        r = super().click(sel, min(wait, 0.12))
        self.hold(max(0.12, min(wait, 0.9)))
        return r

    def type_text(self, sel: str, text: str, cps: float = 22.0, tail: float = 0.4):
        """逐字输入，每个字占 2 帧（成片约 15 字/秒）。"""
        import json as _json
        self.eval("(function(){var el=document.querySelector(%s);if(el){el.focus();"
                  "el.value='';el.dispatchEvent(new Event('input',{bubbles:true}));}})()"
                  % _json.dumps(sel))
        self.snapshot(2)
        for i in range(1, len(text) + 1):
            self.eval("(function(){var el=document.querySelector(%s);if(el){el.value=%s;"
                      "el.dispatchEvent(new Event('input',{bubbles:true}));}})()"
                      % (_json.dumps(sel), _json.dumps(text[:i], ensure_ascii=False)))
            self.snapshot(2)
        self.hold(tail or 0.25)

    def scroll(self, sel: str, y, ms: int = 700):
        """把滚动拆成若干帧逐帧推进，成片里就是平滑滚动。"""
        import json as _json
        sel_expr = "document.querySelector(%s)" % _json.dumps(sel) if sel else "null"
        cur = self.eval(
            "(function(){var el=%s;var h=el||document.scrollingElement||document.documentElement;"
            "return [h.scrollTop,h.scrollHeight,h.clientHeight];})()" % sel_expr) or [0, 0, 0]
        top, total, view = cur
        target = max(0, total - view) if y == "end" else int(y)
        n = max(6, int(ms / 1000.0 * FPS))
        if abs(target - top) < 4:
            self.hold(0.3)
            return
        self.eval("(function(){var el=%s;var h=el||document.scrollingElement;"
                  "document.documentElement.style.scrollBehavior='auto';"
                  "if(el){el.style.scrollBehavior='auto';}})()" % sel_expr)
        for i in range(1, n + 1):
            pos = top + (target - top) * ease(i / float(n))
            self.eval("(function(){var el=%s;var h=el||document.scrollingElement;"
                      "if(h){h.scrollTop=%f;}})()" % (sel_expr, pos))
            self.snapshot(1)
        self.hold(0.2)

    def select_paragraph(self, nchars: int = 26, body_sel: str = "#reader-body"):
        super().select_paragraph(nchars, body_sel)
        self.hold(0.5)

    # ---------------------------------------------------------------- 等待
    def wait_reply(self, log_sel: str = "#agent-log", timeout: float = 240.0,
                   quiet: float = 2.4) -> bool:
        """等回合结束；等待期间每 3 次采样留一帧，成片里是「处理中」的短暂定格。"""
        base = self.eval("(function(){var l=document.querySelector(%s);"
                         "return l?l.textContent.length:0;})()" % json.dumps(log_sel)) or 0
        end = time.time() + timeout
        last_len, last_change = -1, time.time()
        started = time.time()
        i = 0
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
            i += 1
            if i % 3 == 0:
                self.snapshot(6)
            if (pend == 0 and ln > base + 15
                    and time.time() - last_change > quiet
                    and time.time() - started > 2.0):
                self.hold(0.4)
                return True
            time.sleep(0.2)
        return False

    def wait_js(self, cond: str, timeout: float = 120.0) -> bool:
        """等结果真的出现；等待期间稀疏留帧，成片里是短暂的"处理中"。"""
        end = time.time() + timeout
        i = 0
        while time.time() < end:
            i += 1
            if self.eval(cond):
                self.hold(0.4)
                return True
            if i % 3 == 0:
                self.snapshot(6)
            time.sleep(0.25)
        return False

    def wait_text(self, sel: str, expect: str, timeout: float = 60.0) -> bool:
        end = time.time() + timeout
        i = 0
        while time.time() < end:
            t = self.eval("(function(){var e=document.querySelector(%s);"
                          "return e?e.textContent:'';})()" % json.dumps(sel)) or ""
            i += 1
            if i % 3 == 0:
                self.snapshot(6)
            if expect in t:
                self.hold(0.4)
                return True
            time.sleep(0.25)
        return False


def run(name: str) -> dict:
    path = os.path.join(OUTDIR, name + ".mp4")
    rec = FrameRecorder(path)
    d = FilmDriver()
    d.seg = (SEG_ORDER.index(name) + 1, len(SEG_ORDER))
    d.rec = rec
    RL.time = ScreenTime(d)
    t0 = time.time()
    try:
        d.start()
        RL.SEGMENTS[name](d, rec)
    finally:
        dur = rec.stop()
        d.stop()
    meta = {"name": name, "path": path, "duration": round(dur, 2),
            "marks": [[round(t, 2), ch, cap] for t, ch, cap in rec.marks]}
    with open(os.path.join(OUTDIR, name + ".json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    print("== %s：成片 %.1fs（%d 帧），录制耗时 %.0fs"
          % (name, dur, rec.frames, time.time() - t0))
    return meta


def main() -> int:
    args = sys.argv[1:] or ["all"]
    if args == ["all"]:
        args = list(SEG_ORDER)
    for name in args:
        if name not in RL.SEGMENTS:
            print("未知段落:", name)
            return 2
        print("\n>>> %s" % name)
        run(name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
