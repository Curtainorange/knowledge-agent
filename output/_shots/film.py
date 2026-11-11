# -*- coding: utf-8 -*-
"""帧步进录制：不占桌面、稳定 30fps、仍然是真实操作。

为什么不用「实时抓帧 + 桌面采集」：
- 桌面采集（gdigrab）虽然能到 30fps，但会把前台任何窗口都录进去，且必须一直占着屏幕；
- 实时期抓帧（captureScreenshot）受接口限制只有 ~5fps，滚动会一顿一顿。

所以这里把「时间」交给自己控制：每抓一帧就把页面状态推进到下一个时刻
（滚动位置、输入框内容、点击后的状态），再按 30fps 编码。滚动因此是逐像素平滑的，
并且页面上的每个动作依然是真打字、真点击、真等模型返回。
"""
from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import time

import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFont

W, H, FPS = 1920, 1080, 30
BART = 946                      # 字幕条上沿
FONT = r"C:\Windows\Fonts\msyh.ttc"
FONT_B = r"C:\Windows\Fonts\msyhbd.ttc"
FF = imageio_ffmpeg.get_ffmpeg_exe()

_FONTS: dict[tuple[int, bool], ImageFont.FreeTypeFont] = {}


def font(size: int, bold: bool = False):
    key = (size, bold)
    if key not in _FONTS:
        _FONTS[key] = ImageFont.truetype(FONT_B if bold else FONT, size)
    return _FONTS[key]


class FrameRecorder:
    """把帧按固定帧率推进 ffmpeg。hold=n 表示这一帧在成片里占 n 帧。"""

    def __init__(self, path: str):
        self.path = path
        self.ff = None
        self.frames = 0
        self.marks: list[tuple[float, str, str]] = []
        self.started = False
        self._bars: dict[tuple[str, str], Image.Image] = {}

    # ---------------------------------------------------------------- 生命周期
    def start(self):
        if self.started:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.ff = subprocess.Popen(
            [FF, "-y", "-hide_banner", "-loglevel", "error",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "%dx%d" % (W, H),
             "-r", str(FPS), "-i", "-", "-an",
             "-c:v", "libx264", "-preset", "medium", "-crf", "20",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", self.path],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.started = True

    def stop(self) -> float:
        if not self.ff:
            return 0.0
        try:
            self.ff.stdin.close()
        except Exception:
            pass
        try:
            self.ff.wait(timeout=180)
        except Exception:
            self.ff.kill()
        self.ff = None
        return self.frames / float(FPS)

    # ---------------------------------------------------------------- 时间轴
    @property
    def now(self) -> float:
        return self.frames / float(FPS)

    def mark(self, chapter: str, caption: str):
        if self.marks and self.marks[-1][1:] == (chapter, caption):
            return
        self.marks.append((self.now, chapter, caption))
        print("  [%6.2fs] %s — %s" % (self.now, chapter, caption))

    def _current(self) -> tuple[str, str]:
        if not self.marks:
            return "", ""
        return self.marks[-1][1], self.marks[-1][2]

    # ---------------------------------------------------------------- 合成
    def _bar(self, chapter: str, caption: str) -> Image.Image:
        key = (chapter, caption)
        if key not in self._bars:
            layer = Image.new("RGBA", (W, H - BART), (0, 0, 0, 0))
            d = ImageDraw.Draw(layer)
            d.rectangle([0, 0, W, H - BART], fill=(15, 23, 42, 216))
            if chapter:
                d.text((64, 16), chapter, font=font(26), fill=(165, 180, 252, 255))
            if caption:
                d.text((64, 52), caption, font=font(33, True), fill=(255, 255, 255, 255))
            self._bars[key] = layer
        return self._bars[key]

    def _decorate(self, img: Image.Image, idx: str) -> Image.Image:
        chapter, caption = self._current()
        band = img.crop((0, BART, W, H)).convert("RGBA")
        band = Image.alpha_composite(band, self._bar(chapter, caption))
        out = img.copy()
        out.paste(band.convert("RGB"), (0, BART))
        if idx:
            d = ImageDraw.Draw(out)
            d.text((W - 150, BART + 47), idx, font=font(24), fill=(148, 163, 184, 255))
        return out

    def push(self, raw_jpeg: bytes, hold: int = 1, idx: str = ""):
        if not self.started:
            return
        img = Image.open(io.BytesIO(raw_jpeg))
        if img.mode != "RGB":
            img = img.convert("RGB")
        if img.size != (W, H):
            img = img.resize((W, H), Image.BILINEAR)
        frame = self._decorate(img, idx)
        data = frame.tobytes()
        for _ in range(max(1, int(hold))):
            self.ff.stdin.write(data)
            self.frames += 1
