# -*- coding: utf-8 -*-
"""分段录制：真实操作真实界面，ffmpeg 采集桌面 30fps。

用法：
    python run_live.py s01 s02 ...     # 按顺序录制指定段落
    python run_live.py all             # 全部

每段产出 _raw/sXX.mp4（原始录屏）与 _raw/sXX.json（字幕时间轴），
由 compose.py 合成带字幕的成片。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import imageio_ffmpeg  # noqa: E402
from drive import Driver  # noqa: E402

ROOT = r"D:\知识学习智能体"
OUTDIR = os.path.join(ROOT, "output", "认知副驾功能汇报-2026-11-11", "video")
RAW = os.path.join(OUTDIR, "_raw")
FF = imageio_ffmpeg.get_ffmpeg_exe()
BOOK = "dc2ecd47-0e7d-49ac-abac-0bfd962166e3"


class Recorder:
    """桌面采集。只在页面就绪后启动，避免录到浏览器启动的白屏。"""

    def __init__(self, path: str):
        self.path = path
        self.proc = None
        self.t0 = None

    def start(self):
        if self.proc:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.proc = subprocess.Popen(
            [FF, "-y", "-hide_banner", "-loglevel", "error",
             "-f", "gdigrab", "-framerate", "30", "-draw_mouse", "0",
             "-offset_x", "0", "-offset_y", "0", "-video_size", "1920x1080",
             "-i", "desktop",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
             "-pix_fmt", "yuv420p", self.path],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.t0 = time.time()
        time.sleep(0.8)

    def stop(self) -> float:
        if not self.proc:
            return 0.0
        dur = time.time() - (self.t0 or time.time())
        try:
            self.proc.stdin.write(b"q")
            self.proc.stdin.flush()
            self.proc.wait(timeout=25)
        except Exception:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=8)
            except Exception:
                self.proc.kill()
        self.proc = None
        return dur


# ---------------------------------------------------------------- 段落剧本
def s01(d: Driver, rec: Recorder):
    """对话即入口：一句话调出书架，再点进阅读器。"""
    d.set_conv(None)
    d.goto("app.html", 2.6)
    rec.start()
    d.mark("01 对话即入口", "登录后落在对话工作台：不需要先选功能，只有一句输入")
    time.sleep(2.4)
    d.mark("01 对话即入口", "副驾先开口，把已知的处境摆出来，再问往哪儿走")
    time.sleep(2.0)
    d.type_text("#agent-input", "看看我的书架", cps=12)
    d.click("#agent-send")
    ok = d.wait_reply(timeout=150)
    d.mark("01 对话即入口", "一句话调出整座书架，含每本书的章节进度")
    time.sleep(2.4)
    d.scroll("#agent-log", "end", 900)
    time.sleep(2.0)
    d.mark("01 对话即入口", "卡片是入口不是截图：点「继续读」直接落到上次的位置")
    time.sleep(1.6)
    d.click('#agent-log a[href*="reader.html"]', 2.4)
    print("   S1 结果:", ok, "跳到:", d.eval("location.pathname + location.search"))


def s02(d: Driver, rec: Recorder):
    """阅读器：划词存摘录、写想法、本书知识、目录跳章。"""
    d.goto("reader.html?book=" + BOOK, 3.0)
    rec.start()
    d.mark("02 阅读与录入", "txt / epub / pdf 都能读，自动分章，进度精确到章内位置")
    time.sleep(2.4)
    d.scroll("", 260, 1000)
    time.sleep(0.8)
    d.mark("02 阅读与录入", "选中一段文字，浮层出现：存摘录，或者连同想法一起存")
    d.select_paragraph(26)
    time.sleep(1.6)
    d.click('#selection-pop button[data-act="excerpt"]', 1.6)
    print("   摘录后:", d.eval("(function(){var t=document.querySelector('#toast');"
                              "return t?t.textContent:'';})()"))
    d.mark("02 阅读与录入", "存下来的摘录带原文定位，能从「本书知识」再跳回来")
    time.sleep(2.2)
    d.scroll("", 520, 900)
    time.sleep(0.6)
    d.select_paragraph(22)
    time.sleep(1.4)
    d.click('#selection-pop button[data-act="thought"]', 1.2)
    d.type_text("#thought-input", "这一段和上周那次复盘对得上，先存下来。", cps=12)
    d.click("#thought-save", 1.8)
    d.mark("02 阅读与录入", "原文与想法合成一条知识入库，后面能被检索、能被冲突检测读到")
    time.sleep(2.0)
    d.click("#btn-notes", 1.4)
    time.sleep(2.2)
    d.mark("02 阅读与录入", "「本书知识」= 这本书里我录过的全部条目，点任意一条回到那段原文")
    d.scroll("#notes-list", 240, 800)
    time.sleep(1.6)
    d.click("#notes-close", 0.8)
    d.click("#btn-toc", 1.2)
    d.mark("02 阅读与录入", "目录按章节结构生成：epub 走 spine，pdf 优先用书内书签")
    time.sleep(2.0)
    d.click("#toc-list .toc-item:nth-child(3)", 2.0)
    d.mark("02 阅读与录入", "跳章后位置记在库中，下次打开就地续读")
    time.sleep(2.0)
    d.click("#font-up", 0.5)
    d.click("#font-up", 0.6)
    time.sleep(1.4)


def s03(d: Driver, rec: Recorder):
    """知识库：手写一条 → 搜索 → 删除。"""
    d.goto("knowledge.html", 2.6)
    rec.start()
    d.mark("03 知识库", "全部条目的向量化状态、来源分布、标签分布都在这一页")
    time.sleep(2.4)
    d.scroll("", 300, 900)
    d.mark("03 知识库", "除了从阅读里录入，也可以直接手写：标题、正文、标签")
    d.type_text("#k-title", "演示：间隔重复的复习节奏", cps=12)
    d.type_text("#k-content",
                "间隔重复主张在快要遗忘时复习，间隔按 1、3、7、15 天递增，"
                "总耗时比集中复习少，长期留存更高。", cps=20)
    d.type_text("#k-tags", "学习方法,记忆", cps=12)
    d.click("#k-submit", 2.0)
    d.mark("03 知识库", "提交即入库，向量化在后台做；进了检索池才算真正可用")
    time.sleep(2.6)
    d.scroll("", 0, 900)
    d.type_text("#kb-search", "间隔", cps=10)
    time.sleep(2.0)
    d.mark("03 知识库", "搜索是混合检索：向量召回 + 关键词，两路结果再做融合排序")
    time.sleep(2.4)
    d.click('.item button[data-act="del"]', 1.8)
    d.mark("03 知识库", "删除是软删除，原文保留；被删的条目不会被同步任务重新拉回来")
    time.sleep(2.4)


def s04(d: Driver, rec: Recorder):
    """一个会话里连做四件事：确认前提 → 给建议 → 聊书 → 跨书对比。"""
    d.set_conv(None)
    d.goto("app.html", 2.6)
    rec.start()
    d.mark("04 一段对话连做四件事", "开场先确认前提：读它是为了什么、之前接触过没有、希望我怎么帮")
    time.sleep(2.6)
    d.mark("04 一段对话连做四件事", "一次最多两题、全部可点选；我已经在读哪本、读到第几章，它早就知道")
    time.sleep(2.4)
    for label in ("学懂概念", "第一次接触", "把这个概念讲清楚"):
        print("   选", label, _pick(d, label, 1.6))
    d.mark("04 一段对话连做四件事", "三个选项点完，一个字都不用打；前提齐了它才动手")
    ok1 = d.wait_reply(timeout=300)
    d.mark("04 一段对话连做四件事", "建议带出处：能命中原文、能跳回定位的条目才算依据，不够就不给")
    time.sleep(3.2)
    d.scroll("#agent-log", "end", 900)
    time.sleep(2.4)
    d.mark("04 一段对话连做四件事", "同一个会话里换个任务：聊书")
    d.type_text("#agent-input", "聊聊《蛤蟆先生去看心理医生》里「愤怒」是怎么被讲的", cps=12)
    d.click("#agent-send")
    ok2 = d.wait_reply(timeout=300)
    d.mark("04 一段对话连做四件事", "聊书只依据这本书的正文，答不上来会说答不上来")
    time.sleep(3.2)
    d.scroll("#agent-log", "end", 900)
    time.sleep(2.2)
    d.mark("04 一段对话连做四件事", "再换一件：跨书对比。先由代码对齐主题，再让模型判差异")
    d.type_text("#agent-input", "对比《高效能人士的七个习惯》和《把时间当作朋友》", cps=12)
    d.click("#agent-send")
    ok3 = d.wait_reply(timeout=300)
    d.mark("04 一段对话连做四件事", "逐点对比，每一点都标出是哪本书、第几节的说法")
    time.sleep(3.4)
    d.scroll("#agent-log", "end", 900)
    time.sleep(3.0)
    print("   S4:", ok1, ok2, ok3)


def _pick(d: Driver, label: str, wait: float = 1.8) -> str:
    """点确认卡上的某个选项（卡片会重绘，所以每次按文字重新查）。"""
    r = d.eval(
        "(function(){var bs=document.querySelectorAll("
        "'#agent-log button[data-agent-action=\"intake.answer\"]');"
        "for(var i=0;i<bs.length;i++){if(bs[i].textContent.indexOf(%s)>=0){"
        "bs[i].click();return 'ok';}}return 'miss';})()" % json.dumps(label))
    time.sleep(wait)
    return r


def s05(d: Driver, rec: Recorder):
    """补前提 → 按依据给建议 → 跨书对比。"""
    d.set_conv(None)
    d.goto("app.html", 2.6)
    rec.start()
    d.mark("05 前提齐了才给建议", "开场先把前提问清：读它是为了什么、之前接触过没有、希望我怎么帮")
    time.sleep(2.4)
    for label in ("学懂概念", "第一次接触", "把这个概念讲清楚"):
        r = _pick(d, label, 1.6)
        print("   选", label, r)
    d.mark("05 前提齐了才给建议", "三个选项点完，一个字都不用打；槽位齐了它才动手")
    ok1 = d.wait_reply(timeout=300)
    d.mark("05 前提齐了才给建议", "每条建议都带出处：能命中原文、能跳回定位的条目，才算依据")
    time.sleep(3.2)
    d.scroll("#agent-log", "end", 900)
    time.sleep(2.4)
    d.mark("05 跨书对比", "对比不是把两本书丢给模型自由发挥：先用代码对齐主题，再判差异")
    time.sleep(1.6)
    d.type_text("#agent-input", "对比《高效能人士的七个习惯》和《把时间当作朋友》", cps=12)
    d.click("#agent-send")
    ok2 = d.wait_reply(timeout=300)
    d.mark("05 跨书对比", "四个判定：一致、互补、冲突、各有侧重，主题对是代码先配对好的")
    time.sleep(3.4)
    d.scroll("#agent-log", "end", 900)
    time.sleep(3.0)
    print("   S5:", ok1, ok2)


def s06(d: Driver, rec: Recorder):
    """L1 挖掘：库内模糊线索 + 全网找书。"""
    d.goto("mine.html", 2.6)
    rec.start()
    d.mark("05 L1 模糊挖掘", "只记得大概内容也能找回来，不用记标题和标签")
    time.sleep(2.0)
    d.type_text("#mine-input", "之前存过一段讲数据库索引为什么失效的内容", cps=13)
    d.click("#mine-send", 1.0)
    d.wait_js("(function(){var e=document.querySelector('#mine-log');"
              "return !!e && e.textContent.length>50;})()", 150)
    d.mark("05 L1 模糊挖掘", "命中就给原文摘要与位置；线索不够时它会反问，而不是硬猜")
    time.sleep(4.0)
    d.scroll("", 300, 900)
    time.sleep(2.2)
    d.mark("05 L1 模糊挖掘", "另一个模式：连书名都不记得，只记得情节，就让它去网上找")
    d.click('#mode-tabs button[data-mode="web"]', 1.4)
    time.sleep(1.4)
    d.type_text("#find-input", "一个人去咖啡馆，被问了三个关于人生意义的问题", cps=12)
    d.click("#find-send", 1.0)
    d.wait_js("(function(){var e=document.querySelector('#find-log');"
              "return !!e && e.textContent.length>60;})()", 150)
    d.mark("05 L1 模糊挖掘", "候选必须指到真实检索结果，搜不到就说搜不到，不编书名")
    time.sleep(5.0)
    d.scroll("", 520, 900)
    time.sleep(3.0)


def s07(d: Driver, rec: Recorder):
    """L2 冲突：扫描 → 采纳 → 归因。"""
    d.goto("conflicts.html", 2.6)
    rec.start()
    d.mark("06 L2 冲突检测", "打开就能看到已判出的冲突：两侧原句、类型、置信度")
    time.sleep(2.6)
    d.click("#btn-scan", 1.2)
    d.mark("06 L2 冲突检测", "重新扫描只处理新增或改过的条目，不重跑已经判过的那批")
    d.wait_js("(function(){var e=document.querySelector('#scan-result');"
              "return !!e && e.textContent.length>4"
              "&& e.textContent.indexOf('扫描中')<0;})()", 240)
    d.mark("06 L2 冲突检测", "本轮扫描 0 条——没有新增内容，判过的一条都不重算")
    print("   扫描:", (d.eval("(function(){var e=document.querySelector('#scan-result');"
                             "return e?e.textContent:'-';})()") or "")[:80])
    time.sleep(2.4)
    d.mark("06 L2 冲突检测", "置信度低的会触发复核：两次判定不一致，就压到低置信度")
    d.click('#cf-tabs button[data-state="accepted"]', 1.6)
    time.sleep(2.0)
    d.scroll("", 320, 900)
    d.mark("06 L2 冲突检测", "归因要回答三件事：为什么对立、各自在什么前提下成立、怎么验证")
    time.sleep(2.6)
    d.click("#cf-list .attrib-toggle, #cf-list details summary", 2.0)
    time.sleep(14.0)
    d.scroll("", 620, 900)
    time.sleep(2.6)
    d.click('#cf-tabs button[data-state="unseen"]', 1.4)
    time.sleep(2.0)


def s08(d: Driver, rec: Recorder):
    """L3 认知简报。"""
    d.goto("brief.html", 2.6)
    rec.start()
    d.mark("07 L3 认知简报", "先看结构：主题分布、认知深度、大量存在与完全缺失的模式")
    time.sleep(2.0)
    d.click("#brief-generate", 1.0)
    d.wait_text("#brief-note", "分析完成", 120)
    time.sleep(1.6)
    d.mark("07 L3 认知简报", "十来个主题的分部一目了然，哪些是偏科、哪些是空白")
    time.sleep(3.0)
    d.scroll("", 380, 1000)
    time.sleep(2.4)
    d.mark("07 L3 认知简报", "追问都带依据：依据来自上面的主题分布与冲突统计，不是随机发问")
    time.sleep(3.4)
    d.scroll("", 760, 1000)
    time.sleep(3.0)


def s09(d: Driver, rec: Recorder):
    """L4 路径修正：目标 → 计划 → 参数 → 偏离。"""
    d.goto("l4.html", 2.8)
    rec.start()
    d.mark("08 L4 路径修正", "先定目标，带优先级")
    time.sleep(1.8)
    d.type_text("#goal-desc", "三个月内系统学完认知科学基础", cps=13)
    d.click("#goal-form button[type=submit]", 2.4)
    d.mark("08 L4 路径修正", "计划由模型排任务，约束由代码管：每周可用时间、回看比例、任务时长")
    time.sleep(2.0)
    before = d.eval("(function(){var e=document.querySelector('#plan-note');"
                    "return e?e.textContent:'';})()") or ""
    hit = d.eval("(function(){var rows=document.querySelectorAll('#goal-list .topic-row');"
                 "for(var i=0;i<rows.length;i++){"
                 "if(rows[i].textContent.indexOf('认知科学')>=0){"
                 "var b=rows[i].querySelector('button[data-gen]');"
                 "if(b){b.click();return 'ok';}}}return 'miss';})()")
    print("   点生成计划:", hit)
    d.wait_js("(function(){var e=document.querySelector('#plan-note');"
              "return !!e && e.textContent !== %s;})()" % json.dumps(before, ensure_ascii=False),
              240)
    print("   生成计划:", (d.eval("(function(){var e=document.querySelector('#plan-note');"
                                 "return e?e.textContent:'-';})()") or "")[:60])
    d.mark("08 L4 路径修正", "每周只有几小时、第几周该回看第几周，这些写进提示词也核查不了，所以放在代码里")
    time.sleep(3.4)
    d.scroll("", 320, 1000)
    time.sleep(2.6)
    d.mark("08 L4 路径修正", "排期参数改完保存，下次排计划按新参数算，装不下就顺延而不是硬塞")
    old = d.eval("document.querySelector('#param-weekly').value") or "300"
    d.type_text("#param-weekly", str(int(float(old)) + 60), cps=14)
    d.click("#param-save", 2.2)
    time.sleep(2.0)
    d.type_text("#param-weekly", old, cps=14)
    d.click("#param-save", 1.8)
    d.mark("08 L4 路径修正", "偏离检测看的是实际推进与计划的对不上，不是有没有打卡")
    d.click("#dev-check", 1.2)
    d.wait_js("(function(){var e=document.querySelector('#dev-view');"
              "return !!e && e.textContent.trim().length>6;})()", 200)
    d.scroll("", 620, 1000)
    time.sleep(2.6)
    v = d.eval("(function(){var e=document.querySelector('#dev-view');"
               "return e?e.textContent.slice(0,60):'-';})()")
    print("   偏离:", (v or "")[:60])


def s10(d: Driver, rec: Recorder):
    """L5 健康诊断。"""
    d.goto("l5.html", 2.6)
    rec.start()
    d.mark("09 L5 健康诊断", "指标全部在本地算：收藏完成比、活跃度、连续无活动天数、未处理冲突")
    time.sleep(2.6)
    d.scroll("", 320, 900)
    time.sleep(1.6)
    d.mark("09 L5 健康诊断", "模型只负责归因：模式 → 根因 → 可执行动作，并给推理链")
    d.click("#diag-generate", 1.0)
    d.wait_js("(function(){var e=document.querySelector('#diag-note');"
              "return !!e && e.textContent.indexOf('诊断中')<0"
              "&& e.textContent.length>4;})()", 200)
    time.sleep(2.0)
    d.scroll("", 0, 700)
    time.sleep(2.6)
    d.mark("09 L5 健康诊断", "结论可采纳也可否定，否定会被记下来，下一轮不再重复给")
    d.scroll("", 420, 1000)
    time.sleep(2.4)
    d.click("#diag-accept", 1.8)
    d.mark("09 L5 健康诊断", "采纳后这条动作进入待办，和计划、冲突一起构成下一轮的依据")
    time.sleep(2.6)


def s11(d: Driver, rec: Recorder):
    """推送设置 + 阅读日志。"""
    d.goto("notify.html", 2.6)
    rec.start()
    d.mark("10 推送与日志", "推送先过三道闸门：免打扰、内容去重、同类被忽略多次后收敛")
    time.sleep(2.6)
    d.click("#pref-frequency", 0.6)
    d.eval("(function(){var s=document.querySelector('#pref-frequency');"
           "s.value='daily';s.dispatchEvent(new Event('change',{bubbles:true}));})()")
    d.click("#pref-form button[type=submit]", 2.0)
    d.mark("10 推送与日志", "频率三档自己定；站内弹窗与手机微信两条通道，都能单独关掉")
    time.sleep(2.6)
    d.scroll("", 300, 900)
    time.sleep(2.4)
    d.goto("reading_log.html", 2.6)
    d.mark("10 推送与日志", "读了什么、读了多少，按天周月三种粒度看")
    time.sleep(2.4)
    d.click('button[data-group="week"]', 1.8)
    time.sleep(2.6)
    d.scroll("", 280, 900)
    time.sleep(2.2)
    d.click('button[data-group="month"]', 1.8)
    time.sleep(2.6)


def s12(d: Driver, rec: Recorder):
    """按依据给建议：收口。"""
    d.set_conv(None)
    d.goto("app.html", 2.6)
    rec.start()
    d.mark("12 按依据给建议", "最后回到最硬的一条：建议必须带得出处")
    time.sleep(1.8)
    d.type_text("#agent-input", "根据我读过的东西，给我一条这周的学习建议", cps=12)
    d.click("#agent-send")
    ok = d.wait_reply(timeout=300)
    d.mark("12 按依据给建议", "只有能命中原文、能跳回定位的条目，才有资格当依据")
    time.sleep(3.2)
    d.scroll("#agent-log", "end", 900)
    time.sleep(2.0)
    d.mark("12 按依据给建议", "模型引了不存在的 id，整条建议丢弃，而不是删掉 id 保留文案")
    time.sleep(3.2)
    d.scroll("#agent-log", "end", 900)
    time.sleep(3.0)
    print("   S12:", ok)


SEGMENTS = {
    "s01": s01, "s02": s02, "s03": s03, "s04": s04, "s06": s06,
    "s07": s07, "s08": s08, "s09": s09, "s10": s10, "s11": s11,
}


def run(name: str) -> dict:
    rec = Recorder(os.path.join(RAW, name + ".mp4"))
    d = Driver().start()
    meta = {"name": name, "raw": rec.path}
    try:
        SEGMENTS[name](d, rec)
    finally:
        dur = rec.stop()
        meta["duration"] = round(dur, 2)
        meta["start"] = round(rec.t0 or 0, 3)
        meta["marks"] = [[round(t - (rec.t0 or t), 2), ch, cap] for t, ch, cap in d.marks]
        d.stop()
    with open(os.path.join(RAW, name + ".json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    print("== %s 完成：%.1fs，%d 条字幕 -> %s"
          % (name, meta["duration"], len(meta["marks"]), meta["raw"]))
    return meta


def main() -> int:
    args = sys.argv[1:] or ["all"]
    if args == ["all"]:
        args = list(SEGMENTS)
    for name in args:
        if name not in SEGMENTS:
            print("未知段落:", name)
            return 2
        print("\n>>> 录制 %s" % name)
        run(name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
