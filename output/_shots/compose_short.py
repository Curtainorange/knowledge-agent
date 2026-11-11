# -*- coding: utf-8 -*-
"""从十段录屏里剪出 90 秒精华版（同一套帧率与字幕，不重新录制）。"""
from __future__ import annotations

import json
import os
import subprocess

import imageio_ffmpeg

FF = imageio_ffmpeg.get_ffmpeg_exe()
OUT = r"D:\知识学习智能体\output\认知副驾功能汇报-2026-11-11\video"

# (段, 起始秒, 时长) —— 只取每段信息量最大的那一截
PICKS = [
    ("s01", 2.0, 12.0),    # 一句话调出书架
    ("s02", 5.0, 12.0),    # 划词 → 存摘录
    ("s04", 8.0, 14.0),    # 点选前提 → 建议带依据
    ("s06", 18.0, 10.0),   # 全网找书，候选指到真实来源
    ("s08", 8.0, 12.0),    # 认知简报：主题分布与追问
    ("s09", 6.0, 9.0),     # 路径：计划与约束
    ("s10", 2.0, 10.0),    # 健康诊断：归因与推理链
]


def duration(path: str) -> float:
    out = subprocess.run([FF, "-hide_banner", "-i", path], capture_output=True, text=True).stderr
    for line in out.splitlines():
        if "Duration" in line:
            h, m, s = line.split("Duration:")[1].split(",")[0].strip().split(":")
            return int(h) * 3600 + int(m) * 60 + float(s)
    return 0.0


def main() -> int:
    parts = []
    tmp = os.path.join(OUT, "_short")
    os.makedirs(tmp, exist_ok=True)
    for name, start, dur in PICKS:
        src = os.path.join(OUT, name + ".mp4")
        dst = os.path.join(tmp, name + ".mp4")
        subprocess.run(
            [FF, "-y", "-hide_banner", "-loglevel", "error", "-ss", str(start),
             "-t", str(dur), "-i", src,
             "-c:v", "libx264", "-preset", "medium", "-crf", "20",
             "-pix_fmt", "yuv420p", "-an", dst],
            check=True,
        )
        parts.append(dst)
        print("%s %5.1fs +%4.1fs" % (name, start, dur))

    with open(os.path.join(tmp, "list.txt"), "w", encoding="utf-8") as f:
        for item in [os.path.join(OUT, "_head.mp4")] + parts + [os.path.join(OUT, "_tail.mp4")]:
            f.write("file '%s'\n" % item.replace("\\", "/"))

    out = os.path.join(OUT, "认知副驾功能演示-90秒.mp4")
    subprocess.run(
        [FF, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", os.path.join(tmp, "list.txt"), "-c", "copy", "-movflags", "+faststart", out],
        check=True,
    )
    info = subprocess.run([FF, "-hide_banner", "-i", out], capture_output=True, text=True)
    for line in info.stderr.splitlines():
        if "Duration" in line or "Stream" in line:
            print(line.strip())
    print("大小 %.1f MB -> %s" % (os.path.getsize(out) / 1048576, out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
