# -*- coding: utf-8 -*-
"""验证「有头全屏 + gdigrab 桌面采集」这条路能否到 30fps。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time

import imageio_ffmpeg
import requests
from websocket import create_connection
from PIL import Image

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
ROOT = r"D:\知识学习智能体"
PROFILE = os.path.join(ROOT, "output", "_shots", "_profile_kiosk")
OUT = os.path.join(ROOT, "output", "_shots", "test_grab.mp4")


def wait_tab(port=9223, timeout=30.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            for t in requests.get("http://127.0.0.1:%d/json/list" % port, timeout=2).json():
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                    return t["webSocketDebuggerUrl"]
        except Exception:
            pass
        time.sleep(0.3)
    raise SystemExit("CDP 未就绪")


if os.path.isdir(PROFILE):
    shutil.rmtree(PROFILE, ignore_errors=True)

proc = subprocess.Popen(
    [EDGE, "--kiosk", "--no-first-run", "--no-default-browser-check", "--disable-extensions",
     "--hide-scrollbars", "--remote-allow-origins=*", "--remote-debugging-port=9223",
     "--user-data-dir=" + PROFILE, "http://127.0.0.1:8010/login.html"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
try:
    ws = create_connection(wait_tab(), timeout=60)
    i = [0]

    def cmd(method, **params):
        i[0] += 1
        ws.send(json.dumps({"id": i[0], "method": method, "params": params}))
        while True:
            m = json.loads(ws.recv())
            if m.get("id") == i[0]:
                return m.get("result", {})

    cmd("Runtime.enable")
    time.sleep(2.0)
    r = cmd("Runtime.evaluate", expression=(
        "JSON.stringify({w:innerWidth,h:innerHeight,dpr:devicePixelRatio,"
        "sw:screen.width,sh:screen.height})"), returnByValue=True)
    print("视口:", r["result"]["value"])

    exe = imageio_ffmpeg.get_ffmpeg_exe()
    t0 = time.time()
    ff = subprocess.Popen(
        [exe, "-y", "-hide_banner", "-loglevel", "error",
         "-f", "gdigrab", "-framerate", "30", "-draw_mouse", "0",
         "-offset_x", "0", "-offset_y", "0", "-video_size", "1920x1080",
         "-i", "desktop",
         "-t", "6", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
         "-pix_fmt", "yuv420p", OUT],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    err = ff.communicate()[1].decode("utf-8", "ignore")
    print("录制耗时 %.1fs" % (time.time() - t0))
    if err.strip():
        print("ffmpeg:", err.strip()[:400])

    rd = imageio.get_reader(OUT)
    meta = rd.get_meta_data()
    n = rd.count_frames() if hasattr(rd, "count_frames") else 0
    print("帧率:", meta.get("fps"), "尺寸:", meta.get("size"), "帧数:", n, "时长: %.2fs" % meta.get("duration", 0))
    frame = rd.get_data(min(40, max(0, n - 1)))
    rd.close()
    Image.fromarray(frame).save(os.path.join(ROOT, "output", "_shots", "test_grab.png"))
    print("已抽帧 test_grab.png")
finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
