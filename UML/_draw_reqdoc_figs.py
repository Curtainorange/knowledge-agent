# -*- coding: utf-8 -*-
"""生成《需求分析文档》所需配图（14 张，200 DPI，风格统一）。
输出目录：D:\\知识学习智能体\\UML\\diagrams\\reqdoc\\
"""
import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Rectangle, FancyArrowPatch, Circle, Ellipse
from matplotlib.lines import Line2D
import matplotlib.font_manager as fm

OUT = r"D:\知识学习智能体\UML\diagrams\reqdoc"
os.makedirs(OUT, exist_ok=True)

for cand in ["Microsoft YaHei", "SimHei", "SimSun"]:
    try:
        fm.findfont(fm.FontProperties(family=cand), fallback_to_default=False)
        plt.rcParams["font.sans-serif"] = [cand]
        break
    except Exception:
        continue
plt.rcParams["axes.unicode_minus"] = False

C_MAIN = "#1565C0"
C_ACC = "#00897B"
C_WARN = "#B54708"
C_LINE = "#8A94A6"
C_FILL_1 = "#EAF3FF"
C_FILL_2 = "#EAF8F0"
C_FILL_3 = "#FFF6E5"
C_GRAY = "#EEF0F6"


def canvas(w, h):
    fig = plt.figure(figsize=(w, h), dpi=200)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_axis_off()
    return fig, ax


def save(fig, name):
    p = os.path.join(OUT, name)
    fig.savefig(p, dpi=200, facecolor="white")
    plt.close(fig)
    from PIL import Image
    print("  %-26s %s" % (name, Image.open(p).size))


def box(ax, x, y, w, h, text, face=C_FILL_1, edge=C_MAIN, fs=10, bold=False, radius=0.9):
    ax.add_patch(FancyBboxPatch((x - w / 2, y - h / 2), w, h,
                                boxstyle="round,pad=0.12,rounding_size=%s" % radius,
                                facecolor=face, edgecolor=edge, lw=1.3, zorder=3))
    ax.text(x, y, text, ha="center", va="center", fontsize=fs,
            fontweight="bold" if bold else "normal", color="#1F2937",
            linespacing=1.45, zorder=4)


def arrow(ax, p1, p2, color=C_LINE, lw=1.4, style="-|>", rad=0.0, zorder=2):
    ax.add_patch(FancyArrowPatch(p1, p2, arrowstyle=style, mutation_scale=12,
                                 lw=lw, color=color, zorder=zorder,
                                 connectionstyle="arc3,rad=%s" % rad,
                                 shrinkA=2, shrinkB=2))


def title(ax, x, y, text, fs=13):
    ax.text(x, y, text, ha="center", va="center", fontsize=fs, fontweight="bold",
            color="#111827")


# 图内标题已由文档排版承担，此处置空
def _noop(*a, **k):
    return None



# ---------------------------------------------------------------- 流程链图
def flow_fig(name, steps, w=7.6, cols=4, note=None, face=C_FILL_1, edge=C_MAIN):
    rows = (len(steps) + cols - 1) // cols
    bh, gap_y, gap_x = 13.0, 7.5, 2.0
    h_units = rows * (bh + gap_y) + (7 if note else 0)
    fig, ax = canvas(w, w * h_units / 100.0)
    ax.set_xlim(0, 100)
    ax.set_ylim(0, h_units)
    bw = 100.0 / cols - gap_x * 1.4
    for i, s in enumerate(steps):
        r, c = divmod(i, cols)
        x = 4 + bw / 2 + c * (bw + gap_x * 1.4)
        y = h_units - 8 - r * (bh + gap_y)
        box(ax, x, y, bw, bh, s, face=face, edge=edge, fs=9,
            bold=(i == 0 or i == len(steps) - 1))
        if i < len(steps) - 1:
            nr, nc = divmod(i + 1, cols)
            nx = 4 + bw / 2 + nc * (bw + gap_x * 1.4)
            if nr == r:
                arrow(ax, (x + bw / 2 + 0.6, y), (nx - bw / 2 - 0.6, y))
            else:
                ny = h_units - 8 - nr * (bh + gap_y)
                arrow(ax, (x, y - bh / 2 - 0.4), (x, ny + bh / 2 + 0.4))
    if note:
        ax.text(50, 3.0, note, ha="center", va="center", fontsize=8, color="#6B7280")
    save(fig, name)


# ---------------------------------------------------------------- 1 上下文图
def fig_context():
    fig, ax = canvas(7.2, 5.0)
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 70)
    ax.add_patch(Rectangle((30, 12), 40, 40, facecolor="#F5F9FF",
                           edgecolor=C_MAIN, lw=1.8, zorder=1))
    ax.text(50, 47.5, "认知副驾系统", ha="center", fontsize=11.5, fontweight="bold",
            color=C_MAIN, zorder=4)
    for i, t in enumerate(["L1 认知挖掘", "L2 冲突检测", "L3 认知助产",
                           "L4 路径修正", "L5 偏误诊断"]):
        box(ax, 50, 41.0 - i * 6.4, 28, 5.2, t, face="white", edge=C_MAIN, fs=8.2)
    box(ax, 12, 42, 20, 9, "学习者\n（登录用户）", face=C_FILL_3, edge="#C87F0A", fs=9, bold=True)
    box(ax, 88, 42, 20, 9, "知识源平台\n（本地书籍 /\n微信读书）", face=C_FILL_2, edge=C_ACC, fs=8.2, bold=True)
    box(ax, 12, 20, 20, 9, "大模型服务\n（DeepSeek API）", face=C_FILL_3, edge=C_WARN, fs=8.2, bold=True)
    box(ax, 88, 20, 20, 9, "推送通道\n（站内通知）", face=C_FILL_2, edge=C_ACC, fs=8.2, bold=True)
    arrow(ax, (22.5, 42), (29.5, 38))
    arrow(ax, (77.5, 42), (70.5, 38))
    arrow(ax, (22.5, 21), (29.5, 25))
    arrow(ax, (77.5, 21), (70.5, 25))
    ax.text(26, 45.5, "意图 / 反馈", fontsize=7.4, color="#6B7280", ha="center")
    ax.text(74, 45.5, "只读同步", fontsize=7.4, color="#6B7280", ha="center")
    ax.text(26, 22.5, "推理调用", fontsize=7.4, color="#6B7280", ha="center")
    ax.text(74, 22.5, "任务投递", fontsize=7.4, color="#6B7280", ha="center")
    _noop(ax, 50, 65, "图 1  系统上下文图（System Context）")
    save(fig, "fig01_context.png")


# ---------------------------------------------------------------- 2 架构图
def fig_arch():
    fig, ax = canvas(7.4, 4.8)
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 62)
    layers = [
        ("用户交互层", ["Web 对话工作台", "功能页（13 个）", "PWA / 移动端"], C_FILL_3, "#C87F0A"),
        ("应用服务层", ["对话编排 router / copilot", "会话与卡片协议", "任务调度与 worker"], C_FILL_1, C_MAIN),
        ("领域能力层", ["L1 挖掘", "L2 冲突", "L3 简报", "L4 计划", "L5 诊断"], C_FILL_2, C_ACC),
        ("数据与模型层", ["关系库", "向量索引", "书籍文件", "LLM 网关（唯一收口）", "成本与日志"], C_GRAY, "#475467"),
    ]
    y = 50
    for lname, items, face, edge in layers:
        ax.add_patch(Rectangle((16, y - 7), 82, 11, facecolor=face, edgecolor=edge, lw=1.3, zorder=1))
        ax.text(15, y - 1.5, lname, ha="right", va="center", fontsize=9, fontweight="bold", color="#374151")
        n = len(items)
        w = 78.0 / n
        align = {3: 24, 5: 15}[n]
        for i, it in enumerate(items):
            x = 18 + i * (w + 0.8) + w / 2
            box(ax, x, y - 1.5, align, 7.0, it, face="white", edge=edge, fs=7.6)
        y -= 12.6
    _noop(ax, 50, 56.5, "图 2  系统总体架构图")
    save(fig, "fig02_arch.png")


# ---------------------------------------------------------------- 3 功能架构
def fig_funcarch():
    fig, ax = canvas(7.2, 4.6)
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 60)
    ax.add_patch(Rectangle((4, 41), 92, 12, facecolor=C_FILL_3, edgecolor="#C87F0A", lw=1.3))
    ax.text(50, 50.5, "交互入口：对话工作台（自然语言）/ 功能页", ha="center", fontsize=9.5,
            fontweight="bold", color="#8A5B00")
    ax.text(5, 34.5, "五级核心能力（主动性由低到高）", ha="left", fontsize=9.5,
            fontweight="bold", color="#374151")
    caps = [("L1\n认知挖掘", "模糊意图\n多轮澄清"), ("L2\n冲突检测", "主动扫描\n实时检测"),
            ("L3\n认知助产", "周简报\n深度追问"), ("L4\n路径修正", "偏离监测\n重规划"),
            ("L5\n偏误诊断", "健康报告\n归因诊断")]
    for i, (t1, t2) in enumerate(caps):
        x = 15 + i * 17.6
        box(ax, x, 28.5, 16.4, 9.2, t1, face=C_FILL_1, edge=C_MAIN, fs=8.6, bold=True)
        box(ax, x, 17.5, 16.4, 8.4, t2, face="white", edge=C_MAIN, fs=7.6)
    ax.add_patch(Rectangle((4, 4), 92, 9, facecolor=C_FILL_2, edgecolor=C_ACC, lw=1.3))
    ax.text(50, 8.5, "支撑能力：知识库接入（UC-G-01）　｜　推送偏好管理（UC-G-02）",
            ha="center", va="center", fontsize=8.8, fontweight="bold", color="#116B5E")
    _noop(ax, 50, 56.5, "图 3  功能架构图")
    save(fig, "fig03_funcarch.png")


# ---------------------------------------------------------------- 4 用例图
def fig_usecase():
    fig, ax = canvas(7.4, 5.2)
    ax.set_xlim(0, 104)
    ax.set_ylim(0, 74)
    ax.add_patch(Rectangle((16, 7), 68, 58, fill=False, edgecolor="#475467", lw=1.6))
    ax.text(50, 66.2, "认知副驾系统边界", ha="center", fontsize=9.5, color="#111827")

    def actor(x, y, label, color):
        ax.add_patch(Circle((x, y + 2.6), 1.4, fill=False, ec=color, lw=1.4))
        ax.add_line(Line2D([x, x], [y + 1.2, y - 1.8], color=color, lw=1.4))
        ax.add_line(Line2D([x - 2.2, x + 2.2], [y + 0.2, y + 0.2], color=color, lw=1.4))
        ax.add_line(Line2D([x - 1.8, x, x + 1.8], [y - 4.4, y - 1.8, y - 4.4], color=color, lw=1.4))
        ax.text(x, y - 6.0, label, ha="center", va="top", fontsize=8.6, color="#1F2937")

    actor(7, 40, "学习者\n(U1)", "#C87F0A")
    actor(96, 18, "知识源\n平台 (S1)", C_ACC)

    # 行 y / 该行 [(编号+名称, 列 x)] / 组名
    rows = [
        (58, "支撑能力", [("UC-G-01\n知识库接入", 33), ("UC-G-02\n推送偏好管理", 64)]),
        (49, "L1 认知挖掘", [("UC-L1-01\n模糊意图挖掘定位", 48)]),
        (40, "L2 冲突检测", [("UC-L2-01\n知识冲突主动扫描", 27),
                          ("UC-L2-02\n新内容实时检测", 50),
                          ("UC-L2-03\n冲突反馈", 73)]),
        (30, "L3 认知助产", [("UC-L3-01\n周认知简报生成", 33), ("UC-L3-02\n深度追问生成", 64)]),
        (21, "L4 路径自我修正", [("UC-L4-01\n计划制定与重规划", 33), ("UC-L4-02\n行为偏离监测与干预", 64)]),
        (12, "L5 偏误诊断", [("UC-L5-01\n月学习健康报告", 33), ("UC-L5-02\n学习归因诊断", 64)]),
    ]
    pos = {}
    for gy, gname, items in rows:
        ax.text(16.8, gy + 4.6, gname, ha="left", fontsize=7.2, color="#6B7280")
        n = len(items)
        w = 20.0 if n == 3 else 24.0
        for txt, gx in items:
            ax.add_patch(Ellipse((gx, gy), w, 8.2, facecolor=C_FILL_1,
                                 edgecolor=C_MAIN, lw=1.0, zorder=3))
            ax.text(gx, gy, txt, ha="center", va="center", fontsize=6.8,
                    color="#111827", linespacing=1.5, zorder=4)
            pos[txt.split("\n")[0]] = (gx, gy, w)

    for code, (x, y, w) in pos.items():
        ax.add_line(Line2D([9.6, x - w / 2 - 0.5], [40, y], color="#B6BEC9", lw=0.8, zorder=2))
    ax.add_line(Line2D([93.6, pos["UC-G-01"][0] + pos["UC-G-01"][2] / 2 + 0.5],
                       [18, pos["UC-G-01"][1] - 2.4], color=C_ACC, lw=0.9, zorder=2))
    _noop(ax, 52, 71.5, "图 4  系统用例图")
    save(fig, "fig04_usecase.png")


# ---------------------------------------------------------------- 5 ER 图
def fig_er():
    fig, ax = canvas(7.4, 5.8)
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 82)

    def ent(x, y, w, h, name, attrs, face=C_FILL_1, edge=C_MAIN):
        ax.add_patch(FancyBboxPatch((x - w / 2, y - h / 2), w, h,
                                    boxstyle="round,pad=0.1,rounding_size=0.6",
                                    facecolor="white", edgecolor=edge, lw=1.2, zorder=3))
        ax.add_patch(Rectangle((x - w / 2, y + h / 2 - 4.4), w, 4.4,
                               facecolor=face, edgecolor=edge, lw=1.2, zorder=4))
        ax.text(x, y + h / 2 - 2.2, name, ha="center", va="center", fontsize=8.0,
                fontweight="bold", color="#1F2937", zorder=5)
        for i, a in enumerate(attrs):
            ax.text(x - w / 2 + 1.0, y + h / 2 - 7.0 - i * 3.1, "· " + a,
                    ha="left", va="center", fontsize=6.4, color="#4B5563", zorder=5)

    ent(16, 62, 26, 17, "User 用户", ["userId", "推送频率", "加密 key"], C_FILL_3, "#C87F0A")
    ent(50, 62, 26, 17, "KnowledgeItem 知识条目", ["标题 / 正文", "来源 / 标签", "阅读进度"])
    ent(84, 62, 27, 15, "Claim 核心主张", ["claimId", "text", "embedding"], C_FILL_2, C_ACC)
    ent(84, 38, 27, 15, "Conflict 观点冲突", ["类型 / 细节", "置信度", "用户状态"], C_FILL_2, C_ACC)
    ent(50, 38, 26, 15, "LearningGoal 学习目标", ["描述 / 期限", "优先级", "是否达成"])
    ent(16, 38, 26, 15, "LearningPlan 学习计划", ["目标关联", "结构化内容", "版本"])
    ent(16, 13, 26, 15, "PlanTask 计划任务", ["周次 / 主题", "状态", "关联条目"])
    ent(50, 13, 26, 15, "CognitiveDiagnosis 诊断", ["行为模式", "归因 / 置信度", "推理链"])
    ent(84, 13, 27, 14, "PushLog 推送日志", ["类型 / 渠道", "内容 hash", "用户反馈"], C_GRAY, "#475467")

    def link(p1, p2, label, rad=0.0, off=(0, 1.7)):
        arrow(ax, p1, p2, color=C_LINE, lw=1.0, style="-", rad=rad, zorder=2)
        ax.text((p1[0] + p2[0]) / 2 + off[0], (p1[1] + p2[1]) / 2 + off[1], label,
                ha="center", va="center", fontsize=6.8, color="#6B7280")

    link((29, 62), (37, 62), "1 : N")
    link((63, 62), (70.5, 62), "1 : N")
    link((84, 54.5), (84, 45.5), "N : 1", off=(5.0, 0))
    link((29, 38), (37, 38), "1 : N")
    link((16, 30.5), (16, 20.5), "1 : N", off=(5.0, 0))
    link((50, 30.5), (50, 45.5), "N : 1", off=(5.0, 0))
    link((63, 38), (70.5, 38), "N : N")
    link((84, 30.5), (84, 20), "1 : N", off=(5.0, 0))
    _noop(ax, 50, 79, "图 5  核心数据实体关系图（E-R）")
    save(fig, "fig05_er.png")


# ---------------------------------------------------------------- 6 状态迁移
def fig_state():
    fig, ax = canvas(7.2, 3.2)
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 42)
    box(ax, 16, 24, 20, 10, "UNSEEN\n（未查看）", face=C_FILL_1, edge=C_MAIN, fs=9, bold=True)
    box(ax, 50, 24, 20, 10, "IGNORED\n（已忽略）", face=C_FILL_3, edge="#C87F0A", fs=9, bold=True)
    box(ax, 84, 24, 20, 10, "ACCEPTED\n（已采纳）", face=C_FILL_2, edge=C_ACC, fs=9, bold=True)
    arrow(ax, (26.5, 24), (39.5, 24), color="#C87F0A")
    ax.text(33, 26.8, "忽略", ha="center", fontsize=7.8, color="#8A5B00")
    arrow(ax, (60.5, 24), (73.5, 24), color=C_ACC)
    ax.text(67, 26.8, "采纳", ha="center", fontsize=7.8, color="#116B5E")
    arrow(ax, (16, 18.8), (16, 9))
    arrow(ax, (16, 9), (50, 9))
    arrow(ax, (50, 9), (50, 18.8))
    ax.text(33, 5.6, "同类型忽略累计 ≥ 3 次 → 收敛（不再主动推荐）", ha="center",
            fontsize=7.8, color="#B54708")
    arrow(ax, (84, 18.8), (84, 9))
    arrow(ax, (84, 9), (66, 9))
    arrow(ax, (66, 9), (66, 18.8))
    ax.text(76, 12.4, "重新激活", ha="center", fontsize=7.4, color="#6B7280")
    _noop(ax, 50, 38.5, "图 6  冲突状态迁移图")
    save(fig, "fig06_state.png")


if __name__ == "__main__":
    print("generating figures ...")
    fig_context()
    fig_arch()
    fig_funcarch()
    fig_usecase()
    fig_er()
    fig_state()
    flow_fig("fig07_mainflow.png",
             ["知识接入", "认知加工\n（向量化 + 主张提取）",
              "主动服务\n（挖掘 / 冲突 / 简报 / 干预 / 诊断）",
              "用户反馈\n（忽略 / 采纳 / 调整）", "闭环修正\n（计划重规划 / 推荐收敛）"],
             cols=5, w=8.4,
             note="总体业务主线：从「知识存储」到「认知转化」的完整链路")
    flow_fig("fig08_ingest.png",
             ["学习者授权接入知识源", "系统建立连接并全量同步原文",
              "向量化与核心主张提取\n（超大条目分块处理）", "回传同步结果\n（失败可重试，不阻塞已有数据）"],
             cols=4, note="对应 UC-G-01 知识库接入")
    flow_fig("fig09_l1.png",
             ["学习者输入碎片化线索", "解析产生多个候选假设", "候选唯一？→ 直接交付",
              "候选不唯一 → 发起多轮追问", "学习者回答缩小范围", "循环至定位\n返回内容 + 阅读历史提醒"],
             cols=3, note="对应 UC-L1-01 模糊意图挖掘定位（无法回答时按相似度兜底）")
    flow_fig("fig10_l2.png",
             ["触发：每周定时扫描 / 新收藏实时检测", "提取核心主张", "语义比对与逻辑分析",
              "识别矛盾 / 互补 / 断层", "低置信度过滤\n（不入库不推送）", "形成冲突候选并推送提醒",
              "学习者反馈：忽略 / 采纳", "反馈用于收敛与信任度量\n（同类型忽略 ≥3 次自动收敛）"],
             cols=4, note="对应 UC-L2-01 / 02 / 03 冲突检测")
    flow_fig("fig11_l3.png",
             ["周认知简报：分析主题分布与认知深度", "识别「大量存在 / 完全缺失」模式",
              "生成 2-3 个启发式深度追问", "汇总本周冲突 → 推送简报",
              "深度追问：问答完成或新收藏后", "基于缺失项生成 1 个衔接性追问",
              "附数据依据推送 → 承接新一轮对话"],
             cols=4, note="对应 UC-L3-01 / 02 认知助产", face=C_FILL_2, edge=C_ACC)
    flow_fig("fig12_l4.png",
             ["学习者设定目标\n（含期限与优先级）", "系统拆解为周维度任务计划", "回传确认 / 修改",
              "计划生效 → 进入行为监测", "触发：连续 N 天未执行 / 模式突变 / 主动请求",
              "聚合行为 → 分析偏离原因", "生成路径调整建议 → 发起干预对话",
              "同意 → 重拆计划；拒绝 → 保留并记录偏好"],
             cols=4, note="对应 UC-L4-01 / 02 路径自我修正")
    flow_fig("fig13_l5.png",
             ["月健康报告：批量分析行为数据", "多因素归因", "形成诊断报告\n（置信度 + 推理链）",
              "推送并询问是否采纳", "采纳 → 方案拆为任务写入计划\n（与 L4 闭环）",
              "主动诊断：学习者询问\n「我为什么学不好」", "链式归因",
              "返回诊断 + 可执行方案\n（可选写入计划）"],
             cols=4, note="对应 UC-L5-01 / 02 偏误诊断", face=C_FILL_2, edge=C_ACC)
    flow_fig("fig14_loop.png",
             ["诊断（L5）", "方案写入计划（L4）", "行为监测", "偏离干预", "新行为数据", "再诊断"],
             cols=6, w=8.6, face=C_FILL_3, edge="#C87F0A",
             note="核心闭环：形成「越用越懂用户」的自进化学习闭环")
    print("done ->", OUT)
