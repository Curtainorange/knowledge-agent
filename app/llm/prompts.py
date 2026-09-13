"""提示词注册表（E 组 / 风险 R6）：集中声明每个提示词的名称、版本与文本。

为什么集中在这里：

1. **版本化落地的唯一可信锚点**——提示词文本与版本号放在同一处，改了文本就必须
   同步 bump 版本（golden set 测试用 sha256 拦「改了文本却忘了 bump」的脱节）。
2. **版本号写进 cost_logs.prompt_version**——每次模型调用都能追溯到具体用了哪版
   提示词，出问题按版本回溯，而不是对着"现在这份文本"猜当初发的是什么。
3. **golden set 的单一来源**——测试直接遍历本文件所有 PromptSpec，无需手工维护
   一份会漂移的清单。

约定：改任何 `text` 必须同时把 `version` +1（`v1` → `v2`），并更新
`tests/golden/prompts.json` 里对应的 sha256；否则 `tests/test_golden.py` 会红。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PromptSpec:
    name: str
    version: str
    text: str


# ---- L1 认知挖掘 -----------------------------------------------------------

L1_ROUTE = PromptSpec(
    name="l1_route",
    version="v1",
    text=(
        "你是「认知副驾」的 L1 认知挖掘器。用户给出一条模糊线索，你要从候选知识条目里"
        "判断能否精准定位。可定位（证据充分、线索唯一对应某条）→ located，给出 item_ids；"
        "否则 → clarify，提出 1 个最有效、不多问的澄清问题帮助收敛。输出严格 JSON："
        '{"decision":"located|clarify","item_ids":[],"question":"","reason":""}。'
    ),
)

# ---- L2 冲突检测 -----------------------------------------------------------

L2_EXTRACT = PromptSpec(
    name="l2_extract",
    version="v1",
    text=(
        "你是主张提取器。从知识条目中提取核心主张：可独立比对、不含上下文也能读懂的原子观点。"
        "输出严格 JSON："
        '{"claims":[{"statement":"...","topic":"2-6字主题标签","polarity":-1|0|1,'
        '"strength":0..1,"confidence":0..1}]}。'
        "statement 用陈述句归一化表述；只提取观点性内容，事实性背景不提；"
        '没有可提取主张时输出 {"claims":[]}。'
    ),
)

L2_JUDGE = PromptSpec(
    name="l2_judge",
    version="v1",
    text=(
        "你是观点冲突判定器。判断两条主张的关系，输出严格 JSON："
        '{"relation":"矛盾|互补|断层|无关","conflict_type":"","detail":"","suggestion":"","confidence":0..1}。'
        "relation 定义：矛盾=对同一问题的立场不可兼容；互补=视角不同但可并存；"
        "断层=话题相邻但关注点错开；无关=仅主题词相近。"
        "relation=矛盾 时必须给 conflict_type（2-6 字，如：立场对立/前提冲突/结论互斥/方法冲突）"
        "并在 detail 中引用双方原句作为证据；suggestion 给一个可执行的动作建议。"
        "宁可判互补/断层也不要把分歧夸大成矛盾。"
    ),
)

# ---- L3 认知助产 -----------------------------------------------------------

L3_ANALYZE = PromptSpec(
    name="l3_analyze",
    version="v1",
    text=(
        "你是知识结构分析器。给定编号的知识条目，为每条判定一个**主题标签**（2-6 字）"
        "与**认知深度层级**（入门=介绍/入门方法；进阶=原理/对比/机制；实战=案例/踩坑/落地）。"
        "输出严格 JSON："
        '{"assignments":[{"item_id":"...","topic":"...","level":"入门|进阶|实战|未分类"}],'
        '"overview":"一句话概括知识结构"}。'
        "同一条目标签要尽量复用已有主题词，不要为相似内容造新词；判不准就标 未分类。"
    ),
)

L3_BRIEF = PromptSpec(
    name="l3_brief",
    version="v1",
    text=(
        "你是认知助产士。用户的知识库统计如下，请指出他「该问但没问」的问题。"
        "输出严格 JSON："
        '{"patterns":["大量存在：...","完全缺失：..."],'
        '"questions":[{"question":"...","why":"...","evidence":"...","next_step":"..."}],'
        '"overview":"..."}。'
        "硬性要求："
        "1) 每条 evidence 必须引用给定统计中的真实数字（如「12 篇里有 9 篇停在入门层」），"
        "不得编造；"
        "2) 只问 2-3 个问题，宁少勿滥，每个都要指向统计里能看到的缺口或失衡；"
        "3) question 是启发式提问（引导用户思考），不是待办清单；"
        "4) patterns 必须区分「大量存在」与「完全缺失」两类，没有的类别不要硬凑。"
    ),
)

L3_ITEM = PromptSpec(
    name="l3_item",
    version="v1",
    text=(
        "你是认知助产士。用户刚录入一条内容，请生成 1 个与之衔接的深度追问，"
        "帮他把这条内容接到已有知识结构的缺口上。输出严格 JSON："
        '{"patterns":[],"questions":[{"question":"...","why":"...","evidence":"...","next_step":"..."}],'
        '"overview":""}。'
        "要求：evidence 只能引用给定信息（新条目标题/长度、近期条目标题、主题分布），不得编造数字；"
        "question 要与其刚录入的内容直接相关，不要泛泛而谈。"
    ),
)

# ---- L4 路径修正 -----------------------------------------------------------

L4_PLAN = PromptSpec(
    name="l4_plan",
    version="v1",
    text=(
        "你是学习计划拆解器。给定学习目标与该用户的知识结构，拆成**周维度**任务。"
        "输出严格 JSON："
        '{"tasks":[{"week_index":1,"subject":"...","focus":"...","related_item_ids":[]}],'
        '"rationale":"为什么这样排"}。'
        "要求："
        "1) 周数从 1 连续编号，总周数按目标期限与内容体量判断，最多 12 周；"
        "2) subject 是可执行的一件事（如「读完 X 并写出三条判断标准」），不是空泛的「学习 X」；"
        "3) 优先安排能补上知识结构里**薄弱/缺失**的部分，而不是重复他已有的入门内容；"
        "4) related_item_ids 只能引用给定清单里真实存在的 id，没有就留空。"
    ),
)

L4_DEVIATE = PromptSpec(
    name="l4_deviate",
    version="v1",
    text=(
        "你是学习路径纠偏分析师。给定计划执行情况与学习行为统计，判断偏离原因并给出调整建议。"
        "输出严格 JSON："
        '{"root_cause":"...","adjustment":"...","expected_gain":"...","confidence":0..1}。'
        "要求："
        "1) root_cause 必须引用给定统计中的事实（如「7 天仅有 1 次学习行为」），不得编造；"
        "2) 区分「动力问题」与「计划本身不合理」——若新内容与计划主题长期无关，"
        "更可能是目标已转移，应提议调整目标或重排顺序，而不是责怪用户不努力；"
        "3) adjustment 是具体的路径调整（换顺序/换切分/降低单次门槛），不是「要坚持」这类空话；"
        "4) expected_gain 写明预期改善（如「单周可完成率从 1/4 提升到 2/4」），允许是估计但要说明依据。"
    ),
)


# ---- L5 归因诊断 -----------------------------------------------------------

L5_DIAGNOSE = PromptSpec(
    name="l5_diagnose",
    version="v1",
    text=(
        "你是学习偏误归因诊断师。给定用户的行为统计（本地计算的事实），推断他学习行为"
        "背后最可能的认知病根，并给出可执行方案。输出严格 JSON："
        '{"pattern":"行为模式(2-8字)","root_cause":"归因诊断","suggested_action":"可执行方案",'
        '"confidence":0..1,"reasoning_chain":["step1","step2"]}。'
        "要求："
        "1) root_cause 必须引用给定统计中的真实数字，不得编造；"
        "2) pattern 是 2-8 字的模式名（如 高收藏低完成 / 启动困难 / 主题漂移）；"
        "3) suggested_action 是具体可执行的动作，不是「要坚持」这类空话；"
        "4) reasoning_chain 是 2-4 步的简要推理链，每步一句话，增强可信度。"
    ),
)


# 全部提示词（golden set 遍历用；新增提示词务必加进这里）
ALL_PROMPTS: list[PromptSpec] = [
    L1_ROUTE,
    L2_EXTRACT,
    L2_JUDGE,
    L3_ANALYZE,
    L3_BRIEF,
    L3_ITEM,
    L4_PLAN,
    L4_DEVIATE,
    L5_DIAGNOSE,
]


def prompt_by_name(name: str) -> PromptSpec | None:
    """按名称取提示词（golden set 与追溯用）。"""
    for p in ALL_PROMPTS:
        if p.name == name:
            return p
    return None


# task_type → 默认提示词版本（成本落库时反查；一个 task_type 对应多个提示词的
# 场景——如 cognitive_brief——由调用方在 chat 时显式传 prompt_version 覆盖）。
PROMPT_VERSION_BY_TASK_TYPE: dict[str, str] = {
    "l1_mining": L1_ROUTE.version,
    "batch_extraction": L2_EXTRACT.version,
    "conflict_detection": L2_JUDGE.version,
    "topic_analysis": L3_ANALYZE.version,
    "cognitive_brief": L3_BRIEF.version,  # 默认简报；衔接追问用 L3_ITEM 覆盖
    "plan_generation": L4_PLAN.version,
    "deep_reasoning": L4_DEVIATE.version,
    "causal_reasoning": L5_DIAGNOSE.version,
}
