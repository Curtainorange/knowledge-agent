# -*- coding: utf-8 -*-
"""生成片头片尾，并把十段录屏拼成成片。"""
from __future__ import annotations

import os
import subprocess
import sys

import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFont

W, H, FPS = 1920, 1080, 30
FONT = r"C:\Windows\Fonts\msyh.ttc"
FONT_B = r"C:\Windows\Fonts\msyhbd.ttc"
FF = imageio_ffmpeg.get_ffmpeg_exe()
OUT = r"D:\知识学习智能体\output\认知副驾功能汇报-2026-11-11\video"

ORDER = ["s01", "s02", "s03", "s04", "s06", "s07", "s08", "s09", "s10", "s11"]


def font(size: int, bold: bool = False):
    return ImageFont.truetype(FONT_B if bold else FONT, size)


def card_png(title: str, sub: str, note: str, path: str):
    img = Image.new("RGB", (W, H), "#FFFFFF")
    d = ImageDraw.Draw(img)
    d.rectangle([96, 372, 104, 706], fill="#4F46E5")
    d.text((152, 378), title, font=font(94, True), fill="#111827")
    d.text((158, 512), sub, font=font(36), fill="#475569")
    d.line([152, 604, 1560, 604], fill="#E2E8F0", width=2)
    d.text((158, 628), note, font=font(27), fill="#94A3B8")
    d.text((158, 972), "认知副驾 · 个人知识学习智能体", font=font(25), fill="#94A3B8")
    img.save(path)


def card_video(png: str, out: str, seconds: float):
    subprocess.run(
        [FF, "-y", "-hide_banner", "-loglevel", "error", "-loop", "1", "-i", png,
         "-t", str(seconds), "-r", str(FPS),
         "-c:v", "libx264", "-preset", "medium", "-crf", "20",
         "-pix_fmt", "yuv420p", "-vf", "scale=%d:%d" % (W, H), out],
        check=True,
    )


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    head_png = os.path.join(OUT, "_head.png")
    tail_png = os.path.join(OUT, "_tail.png")
    card_png("认知副驾", "功能演示 · 2026-11-11",
             "全部画面为真实界面与真实操作，未使用演示动画或剪辑替身", head_png)
    card_png("把收藏的碎片，变成可调用的认知",
             "找得到 · 看得出矛盾 · 问得出问题 · 改得动计划",
             "10 段实操：对话入口 / 阅读录入 / 知识库 / 五层能力 / 推送", tail_png)

    head = os.path.join(OUT, "_head.mp4")
    tail = os.path.join(OUT, "_tail.mp4")
    card_video(head_png, head, 4.0)
    card_video(tail_png, tail, 4.5)

    listing = os.path.join(OUT, "_concat.txt")
    with open(listing, "w", encoding="utf-8") as f:
        for item in [head] + [os.path.join(OUT, s + ".mp4") for s in ORDER] + [tail]:
            f.write("file '%s'\n" % item.replace("\\", "/"))

    out = os.path.join(OUT, "认知副驾功能演示-实操完整版.mp4")
    subprocess.run(
        [FF, "-y", "-hide_banner", "-loglevel", "error",
         "-f", "concat", "-safe", "0", "-i", listing,
         "-c", "copy", "-movflags", "+faststart", out],
        check=True,
    )
    info = subprocess.run([FF, "-hide_banner", "-i", out], capture_output=True, text=True)
    for line in info.stderr.splitlines():
        if "Duration" in line or "Stream" in line:
            print(line.strip())
    print("大小 %.1f MB" % (os.path.getsize(out) / 1048576))
    print("输出:", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
