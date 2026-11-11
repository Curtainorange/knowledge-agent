# -*- coding: utf-8 -*-
"""绘制汇报用的机制示意图（浅色、与产品一致的靛蓝主色）。

产出到 output/认知副驾功能汇报-2026-11-11/assets/。
"""
from __future__ import annotations

import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

OUT = r"D:\知识学习智能体\output\认知副驾功能汇报-2026-11-11\assets"
os.makedirs(OUT, exist_ok=True)

INK = "#1F2937"
MUTED = "#64748B"
PRIMARY = "#4F46E5"
FILL = "#EEF2FF"
FILL2 = "#F8FAFC"
EDGE = "#C7D2FE"
WARN = "#B45309"
WARN_FILL = "#FEF3C7"
OK = "#047857"
OK_FILL = "#D1FAE5"

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False


def canvas(title: str, sub: str = ""):
    fig = plt.figure(figsize=(16, 9), dpi=100)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    ax.axis("off")
    ax.add_patch(plt.Rectangle((0, 0), 100, 100, color="white", zorder=0))
    ax.text(5, 92.5, title, fontsize=27, color=INK, weight="bold", va="center")
    if sub:
        ax.text(5, 86.2, sub, fontsize=14, color=MUTED, va="center")
    ax.plot([5, 95], [82.5, 82.5], color="#E2E8F0", lw=1.4)
    return fig, ax


def box(ax, x, y, w, h, text, *, fs=13, fill=FILL, edge=EDGE, color=INK,
        weight="normal", lw=1.6, radius=1.2, align="center"):
    ax.add_patch(
        FancyBboxPatch(
            (x, y), w, h,
            boxstyle=f"round,pad=0,rounding_size={radius}",
            linewidth=lw, edgecolor=edge, facecolor=fill, zorder=2,
        )
    )
    tx = x + w / 2 if align == "center" else x + 1.6
    ax.text(tx, y + h / 2, text, fontsize=fs, color=color, ha=align,
            va="center", weight=weight, zorder=3, linespacing=1.6)


def arrow(ax, x1, y1, x2, y2, color=PRIMARY, lw=1.8, style="-|>", rad=0.0):
    ax.add_patch(
        FancyArrowPatch(
            (x1, y1), (x2, y2), arrowstyle=style, mutation_scale=16,
            linewidth=lw, color=color, zorder=4,
            connectionstyle=f"arc3,rad={rad}",
        )
    )


# ---------------------------------------------------------------- 1 依据三级
def fig_grounding():
    fig, ax = canvas(
        "依据三级：只有「可溯源」才有资格当建议依据",
        "app/agent/grounding.py —— 「未入库内容不得作为建议依据」的唯一落地处",
    )
    rows = [
        ("l1  已入库", "条目在库里（is_deleted = False）", "否", "只是「库里有」，说明不了什么",
         FILL2, "#E2E8F0", MUTED),
        ("l2  可召回", "语义检索能召回 / 被关键词命中", "否", "召回得到，但引不了",
         FILL2, "#E2E8F0", MUTED),
        ("l3  可溯源", "quote 命中原文 且 能跳回定位", "是", "唯一可进 basis_item_ids",
         FILL, EDGE, PRIMARY),
    ]
    y = 64
    for name, desc, can, note, fill, edge, color in rows:
        box(ax, 5, y, 18, 13, name, fs=15, weight="bold", fill=fill, edge=edge, color=color)
        box(ax, 25, y, 36, 13, desc, fs=13, fill="white", edge="#E2E8F0", color=INK, align="left")
        tag_fill = OK_FILL if can == "是" else "#F1F5F9"
        tag_edge = "#6EE7B7" if can == "是" else "#E2E8F0"
        tag_color = OK if can == "是" else MUTED
        box(ax, 63, y, 8, 13, can, fs=17, weight="bold", fill=tag_fill, edge=tag_edge, color=tag_color)
        box(ax, 73, y, 22, 13, note, fs=12, fill="white", edge="#E2E8F0", color=MUTED, align="left")
        y -= 16

    ax.text(5, 27.5, "模型给了建议 → 代码校验引用的 id 是否真在本次证据池里", fontsize=13, color=INK)
    arrow(ax, 15, 24.5, 15, 20.6, color=PRIMARY)
    box(ax, 20, 8, 32, 12, "引用不实 → 整条建议丢弃\n（不是删 id 保留文案）",
        fs=13, fill=WARN_FILL, edge="#FCD34D", color=WARN, align="left", weight="bold")
    box(ax, 56, 8, 39, 12,
        "理由：文案本身就是靠那条不存在的依据撑起来的，\n删掉 id 等于把幻觉洗成「没有出处的经验」",
        fs=12, fill="white", edge="#E2E8F0", color=MUTED, align="left")
    fig.savefig(os.path.join(OUT, "fig_grounding.png"), facecolor="white")
    plt.close(fig)


# ---------------------------------------------------------------- 2 理解度探查
def fig_probe():
    fig, ax = canvas(
        "理解度探查：把「没摘录」和「没读懂」分开",
        "app/agent/probe.py —— 判定矩阵与复述覆盖率全在代码里算，不交模型",
    )
    heads = ["自评（第 1 轮）", "判断题（第 1 轮）", "复述覆盖率（第 2 轮）", "结论"]
    xs = [5, 30, 47, 67]
    ws = [24, 16, 19, 28]
    for x, w, h in zip(xs, ws, heads):
        box(ax, x, 70, w, 8, h, fs=12.5, fill="#EEF2FF", edge=EDGE, color=PRIMARY, weight="bold")

    rows = [
        ("第一次接触", "—", "—", "guide　先读，不诊断", FILL2, "#E2E8F0", MUTED),
        ("读过 / 能讲", "对", "—", "mastered　已掌握", FILL2, "#E2E8F0", MUTED),
        ("读过 / 能讲", "错", "< 0.4", "gap　理解缺口，可出诊断", WARN_FILL, "#FCD34D", WARN),
        ("读过 / 能讲", "错", "≥ 0.4", "bias　方向对、表述偏", FILL, EDGE, PRIMARY),
    ]
    y = 58
    for r in rows:
        fill, edge, color = r[4], r[5], r[6]
        for i in range(4):
            box(ax, xs[i], y, ws[i], 9, r[i], fs=12.5,
                fill=fill if i == 3 else "white",
                edge=edge if i == 3 else "#E2E8F0",
                color=color if i == 3 else INK)
        y -= 11

    box(ax, 5, 8, 42, 13,
        "第 2 轮只问一部分人\n自评「第一次接触」→ 不追问（没学过，判错正常）\n判断题答对 → 不追问（已证明读懂）",
        fs=12, fill="white", edge="#E2E8F0", color=INK, align="left")
    box(ax, 51, 8, 44, 13,
        "为什么覆盖率不调模型\n同一段话每次判分可能不同，判分变成随机数；\n字面覆盖率可复现、可单测，且判错代价可控",
        fs=12, fill="white", edge="#E2E8F0", color=INK, align="left")
    fig.savefig(os.path.join(OUT, "fig_probe.png"), facecolor="white")
    plt.close(fig)


# ---------------------------------------------------------------- 3 通读 loop
def fig_loop():
    fig, ax = canvas(
        "通读一本书：可靠性来自三件套，而不是模型答得好不好",
        "app/loop/ —— 状态落盘 / 独立验证 / 停止规则",
    )
    cols = [
        ("状态落盘\nstate.py", "每章读完写一次进度\n崩溃后从断点续读\n不重头再来一遍", FILL, EDGE, PRIMARY),
        ("独立验证\nverify.py", "前置检查：章节有正文吗\n结果校验：要点是否落在原文\n不认模型自报的成功", FILL, EDGE, PRIMARY),
        ("停止规则\nrunner.py", "章数上限 / 预算上限\n连续空结果即停\n不会自己跑一整夜", FILL, EDGE, PRIMARY),
    ]
    x = 5
    for name, body, fill, edge, color in cols:
        box(ax, x, 46, 28, 26, "", fill=fill, edge=edge)
        ax.text(x + 14, 66, name, fontsize=14.5, color=color, weight="bold",
                ha="center", va="center", linespacing=1.5, zorder=3)
        ax.text(x + 2.2, 56, body, fontsize=12, color=INK, ha="left", va="center",
                linespacing=2.0, zorder=3)
        x += 31

    box(ax, 5, 30, 90, 10,
        "与用户阅读完全隔离：通读产出只写 book_agent_readings，不碰 books.read_progress，也不往知识库塞任何东西",
        fs=13, fill="#F0FDF4", edge="#86EFAC", color=OK, align="left")
    box(ax, 5, 14, 90, 12,
        "token 口径 = cost_logs 表主键水位线差分（不是按时间窗过滤）：同秒多次调用不漏、不受时区与时钟漂移影响、"
        "还能把同时段别的后台任务排除在外",
        fs=12.5, fill="white", edge="#E2E8F0", color=MUTED, align="left")
    fig.savefig(os.path.join(OUT, "fig_loop.png"), facecolor="white")
    plt.close(fig)


# ---------------------------------------------------------------- 4 对话链路
def fig_pipeline():
    fig, ax = canvas(
        "对话即入口：一句话进去，一张卡片出来",
        "app/agent/router.py → copilot.py → turns.py → cards.py",
    )
    steps = [
        ("一句话", "「帮我找些\n学 AI 的材料」", "white", "#E2E8F0", INK),
        ("意图路由", "模型判定为主\n本地规则只作先验", FILL, EDGE, PRIMARY),
        ("能力编排", "同步直跑\n长任务转后台", FILL, EDGE, PRIMARY),
        ("卡片协议", "25 种卡片\n前端只认 kind", FILL, EDGE, PRIMARY),
        ("会话回写", "轮询刷新\npending 就地改写", FILL, EDGE, PRIMARY),
    ]
    x = 4
    for i, (name, body, fill, edge, color) in enumerate(steps):
        box(ax, x, 47, 16, 25, "", fill=fill, edge=edge)
        ax.text(x + 8, 65.5, name, fontsize=14, color=color, weight="bold",
                ha="center", va="center", zorder=3)
        ax.text(x + 8, 56, body, fontsize=11, color=INK if fill == FILL else MUTED,
                ha="center", va="center", linespacing=1.9, zorder=3)
        if i < len(steps) - 1:
            arrow(ax, x + 16.4, 59.5, x + 18.6, 59.5)
        x += 18.4

    box(ax, 4, 30, 44, 11,
        "判官是模型，关键词不是结论\n关键词命中 ≠ 看懂整句（「我想学 AI，帮我收集材料，再排个计划」）",
        fs=12.5, fill=WARN_FILL, edge="#FCD34D", color=WARN, align="left")
    box(ax, 52, 30, 44, 11,
        "没接住的半句必须说出来\nunhandled → 明说「这件事我暂时做不了」\n而不是静默丢掉",
        fs=12.5, fill="#F0FDF4", edge="#86EFAC", color=OK, align="left")
    box(ax, 4, 13, 92, 11,
        "代价不对等：多问一次模型是几十毫秒加一点钱；判错能力却是去做了用户没要求的事 —— 所以「命中」只用来给线索，不用来下结论",
        fs=12.5, fill="white", edge="#E2E8F0", color=MUTED, align="left")
    fig.savefig(os.path.join(OUT, "fig_pipeline.png"), facecolor="white")
    plt.close(fig)


# ---------------------------------------------------------------- 5 L2 漏斗
def fig_funnel():
    fig, ax = canvas(
        "L2 冲突检测：五级漏斗，模型只在中间两级说话",
        "app/agent/l2_orchestrator.py + l2_judge_quality.py + attribution.py",
    )
    stages = [
        ("① 增量提取主张", "只扫新条目 / 内容变更的条目；书籍通读笔记走「影子主张」，跳过一次提取", 90),
        ("② 近邻召回候选", "主张向量近邻 + 关键词，先缩小到少数可比对", 80),
        ("③ 模型判定", "关系 / 类型 / 细节 / 建议 —— reasoning = on", 70),
        ("④ 校准 + 复核", "置信度用本地信号缩放；证据不足或模型自相矛盾 → 触发复核合议", 60),
        ("⑤ 冲突归因", "背景 / 前提 / 边界 / 可验证判据 —— 独立调用、独立 schema", 50),
    ]
    y = 66
    for name, body, width in stages:
        x = 5 + (90 - width) / 2
        box(ax, x, y, width, 11, "", fill=FILL, edge=EDGE)
        ax.text(x + 2, y + 7.2, name, fontsize=13, color=PRIMARY, weight="bold", zorder=3)
        ax.text(x + 2, y + 3.6, body, fontsize=10.5, color=INK, zorder=3)
        y -= 13

    box(ax, 5, 6, 90, 8,
        "「模型不算术、规则做统计」：置信度校准、复核触发、评测指标（关系准确率 / 矛盾 P·R·F1 / ECE / Brier）全是本地纯函数",
        fs=12, fill="white", edge="#E2E8F0", color=MUTED, align="left")
    fig.savefig(os.path.join(OUT, "fig_funnel.png"), facecolor="white")
    plt.close(fig)


if __name__ == "__main__":
    fig_grounding()
    fig_probe()
    fig_loop()
    fig_pipeline()
    fig_funnel()
    print("done")
