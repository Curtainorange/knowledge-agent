# -*- coding: utf-8 -*-
"""绘制「认知副驾」用例图（两张，按域拆分），200 DPI PNG。
布局精确可控：参与者、用例椭圆、系统边界、关联线、extend 虚线。
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Ellipse, Rectangle, FancyArrowPatch, Circle
from matplotlib.lines import Line2D
import matplotlib.font_manager as fm
import os

DIAG = r"D:\知识学习智能体\UML\diagrams"

# ---------- 字体 ----------
for cand in ["Microsoft YaHei", "SimHei", "SimSun"]:
    try:
        fm.findfont(fm.FontProperties(family=cand), fallback_to_default=False)
        plt.rcParams["font.sans-serif"] = [cand]
        break
    except Exception:
        continue
plt.rcParams["axes.unicode_minus"] = False

C_UC_KNOW = "#EAF3FF"      # 知识资产用例底色
C_UC_ABIL = "#EAF8F0"      # 认知能力用例底色
C_UC_EDGE = "#2E5AAC"
C_ABIL_EDGE = "#1E8449"
C_ACTOR = "#C87F0A"
C_BOUND = "#475467"
C_LINE = "#8A94A6"
C_EXT = "#B54708"


def draw_actor(ax, x, y, label, edge=C_ACTOR, fs=9.5):
    """火柴人参与者。"""
    ax.add_patch(Circle((x, y + 3.0), 1.5, fill=False, ec=edge, lw=1.4, zorder=5))
    ax.add_line(Line2D([x, x], [y + 1.5, y - 2.0], color=edge, lw=1.4, zorder=5))
    ax.add_line(Line2D([x - 2.4, x + 2.4], [y + 0.4, y + 0.4], color=edge, lw=1.4, zorder=5))
    ax.add_line(Line2D([x - 2.0, x, x + 2.0], [y - 5.0, y - 2.0, y - 5.0], color=edge, lw=1.4, zorder=5))
    ax.text(x, y - 6.6, label, ha="center", va="top", fontsize=fs, color="#1F2937", linespacing=1.4)


def draw_usecase(ax, x, y, w, h, text, face, edge, fs=9.0):
    ax.add_patch(Ellipse((x, y), w, h, facecolor=face, edgecolor=edge, lw=1.1, zorder=4))
    ax.text(x, y, text, ha="center", va="center", fontsize=fs, color="#111827", linespacing=1.35, zorder=6)


def link(ax, p1, p2, color=C_LINE, lw=0.7, ls="-", zorder=2):
    ax.add_line(Line2D([p1[0], p2[0]], [p1[1], p2[1]], color=color, lw=lw, ls=ls, zorder=zorder))


def canvas(w_in, h_in):
    fig = plt.figure(figsize=(w_in, h_in), dpi=200)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_axis_off()
    return fig, ax


def save(fig, name):
    path = os.path.join(DIAG, name)
    fig.savefig(path, dpi=200, facecolor="white")
    plt.close(fig)
    from PIL import Image
    im = Image.open(path)
    print("saved %-28s %s" % (name, im.size))


# ============================================================
# 图1　知识资产与账号用例图
# ============================================================
def build_fig1():
    fig, ax = canvas(7.0, 5.9)
    ax.set_xlim(0, 138)
    ax.set_ylim(0, 116)

    # 系统边界
    ax.add_patch(Rectangle((18, 3), 92, 105, fill=False, ec=C_BOUND, lw=1.6, zorder=1))
    ax.text(64, 109.6, "认知副驾（Cognitive Copilot）—— 系统边界", ha="center", va="bottom",
            fontsize=10, color="#111827")

    LX, LY = 7.0, 62.0
    WX, WY = 126.0, 30.0
    draw_actor(ax, LX, LY, "学习者\n（登录用户）")
    draw_actor(ax, WX, WY, "微信读书\n开放平台\n«外部系统»", edge="#1F6FEB")

    U = [
        ("UC-01 注册账号", 34, 92),
        ("UC-02 登录与续期", 64, 92),
        ("UC-03 维护偏好", 94, 92),
        ("UC-04 录入知识条目", 34, 70),
        ("UC-05 管理知识条目", 64, 70),
        ("UC-06 导出知识", 94, 70),
        ("UC-07 上传书籍", 34, 48),
        ("UC-08 阅读书籍", 64, 48),
        ("UC-09 划词记入知识", 94, 48),
        ("UC-10 同步微信读书", 64, 24),
    ]
    W, H = 28, 11
    for text, x, y in U:
        face = C_UC_KNOW
        draw_usecase(ax, x, y, W, H, text, face, C_UC_EDGE)
        link(ax, (LX + 2.4, LY), (x, y))

    link(ax, (WX - 2.4, WY), (64 + H / 2 * 0.75, 24 - 2.0), color="#1F6FEB", lw=1.0)

    ax.text(64, 7.0, "范围：账号与会话、知识条目、书籍、阅读划线、微信读书同步（详见范围表）",
            ha="center", fontsize=7.6, color="#6B7280")
    save(fig, "uc1_knowledge.png")


# ============================================================
# 图2　五层认知能力与支撑用例图
# ============================================================
def build_fig2():
    fig, ax = canvas(7.0, 6.3)
    ax.set_xlim(0, 138)
    ax.set_ylim(0, 124)

    ax.add_patch(Rectangle((18, 3), 92, 113, fill=False, ec=C_BOUND, lw=1.6, zorder=1))
    ax.text(64, 117.6, "认知副驾（Cognitive Copilot）—— 系统边界", ha="center", va="bottom",
            fontsize=10, color="#111827")

    LX, LY = 7.0, 66.0
    MX, MY = 126.0, 34.0
    draw_actor(ax, LX, LY, "学习者\n（登录用户）")
    draw_actor(ax, MX, MY, "DeepSeek\n模型服务\n«外部系统»", edge="#1F6FEB")

    U = [
        ("UC-11 模糊意图挖掘", 36, 100),
        ("UC-12 扫描隐性冲突", 66, 100),
        ("UC-13 处置冲突反馈", 96, 100),
        ("UC-14 翻案误判", 36, 76),
        ("UC-15 生成认知简报", 66, 76),
        ("UC-16 设定学习目标", 96, 76),
        ("UC-17 生成与重排计划", 36, 52),
        ("UC-18 偏离检测与决策", 66, 52),
        ("UC-19 生成归因诊断", 96, 52),
        ("UC-20 采纳诊断结论", 36, 26),
        ("UC-21 接收主动推送", 66, 26),
    ]
    W, H = 28, 11
    pos = {}
    for text, x, y in U:
        draw_usecase(ax, x, y, W, H, text, C_UC_ABIL, C_ABIL_EDGE)
        pos[text.split()[0]] = (x, y)
        link(ax, (LX + 2.4, LY), (x, y))

    # 外部模型服务只与真正调用模型的用例相连
    for key in ["UC-11", "UC-12", "UC-15", "UC-17", "UC-19"]:
        x, y = pos[key]
        link(ax, (MX - 2.4, MY), (x, y), color="#1F6FEB", lw=1.0)

    # extend：UC-20 采纳诊断结论 → UC-19 生成归因诊断
    x1, y1 = pos["UC-20"]
    x2, y2 = pos["UC-19"]
    ax.add_patch(FancyArrowPatch((x1, y1 + H / 2 * 0.78), (x2 - W / 2 * 0.72, y2 - 1.5),
                                 arrowstyle="-|>", mutation_scale=11, lw=1.1,
                                 linestyle=(0, (5, 3)), color=C_EXT, zorder=3))
    ax.text(78, 33.5, "«extend»\n条件：学习者请求采纳\n扩展点：诊断已交付",
            ha="center", va="center", fontsize=7.4, color=C_EXT, linespacing=1.5)

    ax.text(64, 7.0, "L1–L5 五层能力 + 推送巡检；各用例既可由对话工作台自然语言触发，也可由对应功能页触发",
            ha="center", fontsize=7.6, color="#6B7280")
    save(fig, "uc2_capability.png")


if __name__ == "__main__":
    build_fig1()
    build_fig2()
