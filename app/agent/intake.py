"""需求确认状态机（B 模块）：把「先给建议」改成「先确认，再建议」。

## 为什么是槽位驱动，而不是固定 3 轮问答

会议纪要说的是「未确认用户目标、进度、理解程度就发建议」。硬性要求是**信息齐了才发**，
不是「必须问满 3 轮」。所以这里的门禁看的是 `missing`（还缺哪几项），轮数只是上限：

- 用户首句已经说清 5 项 → `missing` 为空 → **0 轮追问，直接出建议**；
- 只说了「我在读《原则》」→ 缺 4 项 → 一轮问 2 项，两轮问完。

写成固定脚本（第 1 轮问 A、第 2 轮问 B）会有两个后果：已经答过的问题再问一遍，
以及用户换个说法系统就接不住。**照槽位算，不照流程算。**

## 为什么规则抽取只抽「高置信度」的

规则匹配必然有误判。误判的代价不对称：漏抽一项只是多问一句，抽错一项会让后续建议
建立在错误前提上（用户以为系统懂他，其实理解反了）。所以每条规则都用**明确短语**，
不用宽泛词库；抽不到就留空让用户点选。

## 为什么「想解决的问题」单独一轮

它是自由文本，信息量最大、也最不可推断。其余四项都有有限取值域（枚举或系统推断），
可以合并成一张卡一次问两个；只有这一项必须留给用户自己说。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

# ---- 取值域（前端按钮与后端校验共用这一份） --------------------------------

GOAL_OPTIONS: tuple[tuple[str, str, str], ...] = (
    ("concept", "学懂概念", "搞清它到底是什么、为什么这样"),
    ("problem", "解决具体问题", "手上有件事要用它来做"),
    ("output", "要做输出", "写作 / 汇报 / 决策要用"),
    ("exam", "考试或证书", "有考纲或证书要求"),
    ("casual", "兴趣泛读", "不求系统，随便看看"),
    ("other", "其他", "一句话说清"),
)

SELF_LEVEL_OPTIONS: tuple[tuple[str, str, str], ...] = (
    ("first", "第一次接触", "没看过，或只看过开头"),
    ("once", "读过一遍", "有印象，但说不全"),
    ("teach", "能讲给别人听", "能自己组织语言讲清楚"),
)

HELP_TYPE_OPTIONS: tuple[tuple[str, str, str], ...] = (
    ("explain", "把这个概念讲清楚", "要类比、要例子"),
    ("evidence", "找出你库里怎么说", "只摆依据，不做评价"),
    ("path", "给我下一步怎么做", "要可执行步骤"),
    ("critique", "挑我理解里的漏洞", "说我哪里想偏了"),
    ("answer_only", "只回答问题别扩展", "问什么答什么"),
)

GOAL_VALUES = frozenset(v for v, _, _ in GOAL_OPTIONS)
SELF_LEVEL_VALUES = frozenset(v for v, _, _ in SELF_LEVEL_OPTIONS)
HELP_TYPE_VALUES = frozenset(v for v, _, _ in HELP_TYPE_OPTIONS)

# 确认项。book/progress 不在此列：它们由系统推断，用户确认即可，缺了也不阻断
# （用户可能只是在看零散笔记，没有"正在读的书"）。
REQUIRED_SLOTS: tuple[str, ...] = ("question", "goal", "self_level", "help_type")

# 出题顺序：先问信息量最大的（具体疑问），再问稳定属性，最后问形式偏好。
# 反过来问会像填表——用户还没说清要解决什么，就被要求选"希望我怎么帮你"。
SLOT_ORDER: tuple[str, ...] = ("question", "goal", "self_level", "help_type")

SLOT_LABELS: dict[str, str] = {
    "question": "想解决的问题",
    "goal": "阅读目标",
    "self_level": "理解程度",
    "help_type": "希望帮助类型",
}

STATUS_COLLECTING = "collecting"
STATUS_READY = "ready"
STATUS_SUGGESTED = "suggested"
STATUS_ABANDONED = "abandoned"

# 单卡最多几题：超过两个问题，卡片就变成表单了
MAX_QUESTIONS_PER_CARD = 2
# 连续跳过的容忍次数：到这个数就停止追问，退成「只问一句」
MAX_SKIPS = 3
QUESTION_MAX = 500
NOTE_MAX = 200


# ---- 槽位容器 ---------------------------------------------------------------


@dataclass
class IntakeContext:
    """一次会话的确认状态。序列化成 dict 存 `conversations.reading_context`。"""

    version: int = 1
    status: str = STATUS_COLLECTING
    round: int = 0
    book: dict | None = None
    progress_source: str = "system"  # system / user / user_corrected
    goal: str = ""
    goal_note: str = ""
    self_level: str = ""
    question: str = ""
    help_type: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    skipped: bool = False
    skips: int = 0
    # 连续跳过到上限后进入精简模式：只问 question，其余不再追问
    reduced_mode: bool = False
    # 确认卡是否正等用户作答。**只由"发卡"置 True**，不由 refresh_missing 推断——
    # 否则一个刚建的、从没发过卡的空上下文也会被当成"确认中"，
    # 结果用户第一句闲聊就被当成对"想解决什么"的回答（这个 bug 真实发生过）。
    awaiting: bool = False
    updated_at: str = ""

    # ---- 序列化 ----

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "status": self.status,
            "round": self.round,
            "book": self.book,
            "progress_source": self.progress_source,
            "goal": self.goal,
            "goal_note": self.goal_note,
            "self_level": self.self_level,
            "question": self.question,
            "help_type": list(self.help_type),
            "missing": list(self.missing),
            "skipped": self.skipped,
            "skips": self.skips,
            "reduced_mode": self.reduced_mode,
            "awaiting": self.awaiting,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "IntakeContext":
        """容错读取：脏数据 / 旧版本一律降级成空上下文，不让确认流程崩在反序列化上。"""
        if not isinstance(data, dict) or not data:
            ctx = cls()
            ctx.refresh_missing()
            return ctx

        ctx = cls(
            version=int(data.get("version") or 1),
            status=str(data.get("status") or STATUS_COLLECTING),
            round=int(data.get("round") or 0),
            book=data.get("book") if isinstance(data.get("book"), dict) else None,
            progress_source=str(data.get("progress_source") or "system"),
            goal=str(data.get("goal") or ""),
            goal_note=str(data.get("goal_note") or ""),
            self_level=str(data.get("self_level") or ""),
            question=str(data.get("question") or ""),
            help_type=[str(x) for x in (data.get("help_type") or []) if str(x).strip()],
            skipped=bool(data.get("skipped")),
            skips=int(data.get("skips") or 0),
            reduced_mode=bool(data.get("reduced_mode")),
            awaiting=bool(data.get("awaiting")),
            updated_at=str(data.get("updated_at") or ""),
        )
        # 取值域可能随版本变化：非法值一律清空，让它重新被问，而不是带着脏值往下走
        if ctx.goal not in GOAL_VALUES:
            ctx.goal, ctx.goal_note = "", ""
        if ctx.self_level not in SELF_LEVEL_VALUES:
            ctx.self_level = ""
        ctx.help_type = [h for h in ctx.help_type if h in HELP_TYPE_VALUES][:2]
        ctx.refresh_missing()
        return ctx

    # ---- 状态 ----

    def refresh_missing(self) -> list[str]:
        """算出还缺哪几项。**这是门禁的唯一判据**，上层不要自己判断。"""
        required = ("question",) if self.reduced_mode else REQUIRED_SLOTS
        self.missing = [slot for slot in SLOT_ORDER if slot in required and not self._filled(slot)]
        if self.missing:
            if self.status in (STATUS_READY, STATUS_SUGGESTED):
                self.status = STATUS_COLLECTING
        elif self.status == STATUS_COLLECTING:
            self.status = STATUS_READY
        self.updated_at = _now_iso()
        return self.missing

    def _filled(self, slot: str) -> bool:
        if slot == "question":
            return bool(self.question.strip())
        if slot == "goal":
            # other 必须带上自由描述才算填了：只选了"其他"什么也没说，等于没答
            return bool(self.goal) and (self.goal != "other" or bool(self.goal_note.strip()))
        if slot == "self_level":
            return bool(self.self_level)
        if slot == "help_type":
            return bool(self.help_type)
        return False

    @property
    def is_ready(self) -> bool:
        return not self.missing

    @property
    def asked_round(self) -> int:
        """已经问过几轮（含开场卡）。用于「最多 3 轮」的上限判断与展示。"""
        return self.round


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


# ---- 从一句话里抽槽位 -------------------------------------------------------

# 每条规则用**明确短语**：宁可漏抽，不要抽错。
_GOAL_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("exam", ("考试", "考证", "考纲", "面试", "考研", "刷题")),
    ("output", (
        "写作", "写文章", "写东西", "写一篇", "写个", "写份", "写点",
        "汇报", "分享给", "做分享", "输出", "做方案", "公众号",
    )),
    ("problem", ("怎么用", "如何用", "怎么落地", "落地", "应用到", "用起来", "解决", "实操", "上手")),
    ("concept", ("搞懂", "搞清楚", "弄明白", "理解一下", "是什么", "什么原理", "原理", "概念")),
    ("casual", ("随便看看", "随便翻", "兴趣", "泛读", "消遣")),
)

_SELF_LEVEL_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("teach", ("能讲给别人", "能讲给", "教过别人", "给团队讲", "很熟", "精通", "烂熟")),
    ("once", ("读过一遍", "看过一遍", "看过", "读过", "有印象", "翻过", "以前学过")),
    ("first", ("第一次", "刚接触", "刚开始", "没接触", "零基础", "入门", "刚看")),
)

_HELP_TYPE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("critique", ("挑毛病", "挑错", "漏洞", "我理解对不对", "看看我理解", "有没有想错")),
    ("answer_only", ("只回答", "别扩展", "不要扩展", "简单说", "直接说")),
    ("path", ("步骤", "下一步", "怎么做", "怎么落地", "路径", "执行计划", "给个计划")),
    ("evidence", ("依据", "出处", "哪里说", "库里怎么", "书里怎么", "证据", "原文")),
    ("explain", ("讲清楚", "解释", "什么意思", "讲讲", "说明白")),
)

_BOOK_PATTERN = re.compile(r"《([^》]{1,40})》")
_CHAPTER_PATTERN = re.compile(r"第\s*([一二三四五六七八九十百零\d]+)\s*章")
_PERCENT_PATTERN = re.compile(r"(\d{1,3})\s*%")
# 这些前缀只是"我要说一件事"的引子，不属于问题本身
_LEADING_NOISE = re.compile(r"^(我想知道|想问一下|想问|请问|帮我看看|帮我看下|想搞清|想了解)[，,：:\s]*")


def extract_slots(text: str) -> dict:
    """从一段用户输入里抽可确定的槽位。只返回抽到的，抽不到就不出现。

    调用方把这些结果 merge 进上下文；**抽错比抽不到更糟**，所以规则刻意保守。
    """
    raw = (text or "").strip()
    if not raw:
        return {}
    out: dict = {}

    book_match = _BOOK_PATTERN.search(raw)
    if book_match:
        out["book_title"] = book_match.group(1).strip()

    chapter = _CHAPTER_PATTERN.search(raw)
    percent = _PERCENT_PATTERN.search(raw)
    progress: dict = {}
    if chapter:
        progress["chapter"] = f"第{chapter.group(1)}章"
    if percent:
        value = min(100, max(0, int(percent.group(1))))
        progress["progress"] = value / 100.0
    if progress:
        out["progress"] = progress

    goal = _first_hit(raw, _GOAL_RULES)
    if goal:
        out["goal"] = goal

    level = _first_hit(raw, _SELF_LEVEL_RULES)
    if level:
        out["self_level"] = level

    help_types = _all_hits(raw, _HELP_TYPE_RULES)
    if help_types:
        out["help_type"] = help_types[:2]

    # 想解决的问题：整句就是答案（剥掉引子即可）。不做摘要、不改写——
    # 用户原话是后续给建议的输入，改写会丢语气与限定条件。
    body = _LEADING_NOISE.sub("", raw).strip()
    if body:
        out["question"] = body[:QUESTION_MAX]

    return out


def _first_hit(text: str, rules: tuple[tuple[str, tuple[str, ...]], ...]) -> str:
    for value, phrases in rules:
        if any(p in text for p in phrases):
            return value
    return ""


def _all_hits(text: str, rules: tuple[tuple[str, tuple[str, ...]], ...]) -> list[str]:
    return [value for value, phrases in rules if any(p in text for p in phrases)]


def merge_slots(ctx: IntakeContext, slots: dict) -> IntakeContext:
    """把抽到的槽位合进上下文。**只补空，不覆盖已答**。

    用户后来改口用 `apply_answer` 显式更正（那里才允许覆盖）；
    自动抽取覆盖用户点选过的答案，等于系统把用户的话改掉。
    """
    if slots.get("goal") and not ctx.goal:
        ctx.goal = slots["goal"]
    if slots.get("self_level") and not ctx.self_level:
        ctx.self_level = slots["self_level"]
    if slots.get("help_type") and not ctx.help_type:
        ctx.help_type = list(slots["help_type"])[:2]
    if slots.get("question") and not ctx.question.strip():
        ctx.question = str(slots["question"])[:QUESTION_MAX]

    title = (slots.get("book_title") or "").strip()
    if title and not ctx.book:
        progress = slots.get("progress") or {}
        ctx.book = {
            "book_id": "",
            "title": title,
            "source": "unknown",
            "progress": progress.get("progress"),
            "chapter": progress.get("chapter") or "",
        }
        ctx.progress_source = "user" if progress else "unknown"
    elif ctx.book and slots.get("progress"):
        # 用户补了进度：以用户为准（不争辩系统推断）
        for key, value in slots["progress"].items():
            ctx.book[key] = value
        ctx.progress_source = "user_corrected"

    ctx.refresh_missing()
    return ctx


def apply_answer(ctx: IntakeContext, slot: str, value) -> IntakeContext:
    """用户显式作答（卡片点选 / 文本框）。

    与 `merge_slots` 的区别：这里是**显式意图**，允许覆盖之前的自动推断。
    """
    if slot == "question":
        ctx.question = str(value or "").strip()[:QUESTION_MAX]
    elif slot == "goal":
        v = str(value or "").strip()
        if v in GOAL_VALUES:
            ctx.goal = v
            if v != "other":
                ctx.goal_note = ""
        else:
            ctx.goal, ctx.goal_note = "other", v[:NOTE_MAX]
    elif slot == "self_level":
        v = str(value or "").strip()
        if v not in SELF_LEVEL_VALUES:
            raise ValueError(f"未知理解程度: {value!r}")
        ctx.self_level = v
    elif slot == "help_type":
        values = value if isinstance(value, (list, tuple)) else [value]
        picked = [str(v) for v in values if str(v) in HELP_TYPE_VALUES][:2]
        if not picked:
            raise ValueError(f"未知帮助类型: {value!r}")
        ctx.help_type = picked
    else:
        raise ValueError(f"未知确认项: {slot!r}")

    ctx.skipped = False
    ctx.skips = 0
    ctx.round += 1
    ctx.refresh_missing()
    return ctx


def mark_skip(ctx: IntakeContext) -> IntakeContext:
    """用户跳过这一轮。

    连续跳过到 `MAX_SKIPS` 后进精简模式：只问「想解决什么」，其余不再追问。
    ——一直追着问五项，比不问更烦；用户已经用行为表达了"别问了"。
    """
    ctx.skipped = True
    ctx.skips += 1
    ctx.round += 1
    if ctx.skips >= MAX_SKIPS and not ctx.reduced_mode:
        ctx.reduced_mode = True
    ctx.refresh_missing()
    return ctx


def mark_ready_for_suggestion(ctx: IntakeContext) -> IntakeContext:
    """建议已产出（或正在产出）——记录状态，避免下次进页面又从头问一遍。"""
    ctx.status = STATUS_SUGGESTED
    ctx.awaiting = False
    ctx.updated_at = _now_iso()
    return ctx


def mark_card_sent(ctx: IntakeContext) -> IntakeContext:
    """确认卡已发出：从现在起用户说的话大概率是在回答，而不是闲聊。

    只有发过卡的上下文才会被 `copilot` 接管成"回答"（见 `_intake_pending`）。
    """
    ctx.awaiting = bool(ctx.missing) and ctx.status != STATUS_SUGGESTED
    ctx.updated_at = _now_iso()
    return ctx


# ---- 出题 -------------------------------------------------------------------


def next_questions(ctx: IntakeContext) -> list[dict]:
    """下一张卡要问哪几道题（≤2）。缺什么问什么，不问已答的。"""
    out: list[dict] = []
    for slot in ctx.missing:
        if slot == "question":
            out.append({
                "id": "question",
                "slot": "question",
                "label": "这次想搞清什么？",
                "hint": "一句话说清就行，比如「怎么把它用到周会上」",
                "kind": "text",
                "options": [],
                "allow_other": True,
                "skippable": True,
            })
        elif slot == "goal":
            out.append({
                "id": "goal",
                "slot": "goal",
                "label": "读这个主要是为了什么？",
                "hint": "",
                "kind": "single",
                "options": [{"value": v, "label": lb, "hint": h} for v, lb, h in GOAL_OPTIONS],
                "allow_other": True,
                "skippable": True,
            })
        elif slot == "self_level":
            out.append({
                "id": "self_level",
                "slot": "self_level",
                "label": "这块内容你之前接触过吗？",
                "hint": "答错也不会怎样，选最接近的就行",
                "kind": "single",
                "options": [{"value": v, "label": lb, "hint": h} for v, lb, h in SELF_LEVEL_OPTIONS],
                "allow_other": False,
                "skippable": True,
            })
        elif slot == "help_type":
            out.append({
                "id": "help_type",
                "slot": "help_type",
                "label": "希望我怎么帮你？",
                "hint": "",
                "kind": "single",
                "options": [{"value": v, "label": lb, "hint": h} for v, lb, h in HELP_TYPE_OPTIONS],
                "allow_other": False,
                "skippable": True,
            })
        if len(out) >= MAX_QUESTIONS_PER_CARD:
            break
    return out


# ---- 文案 -------------------------------------------------------------------


def opening_text(ctx: IntakeContext, *, unseen_conflicts: int = 0) -> str:
    """开场：只问候 + 确认推断，**不给任何行动建议**。

    原实现是「有冲突就问你更认同哪个 / 有目标就问要不要拆计划」，那是在用户还没
    说清诉求时就把动作推过去。现在开场只做一件事：把系统已经知道的摆出来，问一句
    方向。未处理的冲突降级成一句待办提示——它是"事情在等你"，不是"我建议你做"。
    """
    book = ctx.book or {}
    title = (book.get("title") or "").strip()
    chapter = (book.get("chapter") or "").strip()
    progress = book.get("progress")

    if title and chapter:
        head = f"我是你的认知副驾。你最近在读《{title}》，读到{chapter}。"
    elif title and isinstance(progress, (int, float)) and 0 < progress < 1:
        head = f"我是你的认知副驾。你最近在读《{title}》，读到 {int(progress * 100)}%。"
    elif title:
        head = f"我是你的认知副驾。你最近在读《{title}》。"
    else:
        head = "我是你的认知副驾。"

    tail = "今天想接着读，还是先解决一个具体问题？"
    if unseen_conflicts > 0:
        tail += f"\n\n另外还有 {unseen_conflicts} 处观点冲突没处理，要顺手过一遍吗？"
    return head + tail


def confirm_text(ctx: IntakeContext) -> str:
    """复述一句：让用户在进建议前有机会纠正。

    只复述**已答的部分**，不替用户补全没说过的——补全等于编造。
    """
    parts: list[str] = []
    if ctx.question.strip():
        parts.append(f"你要解决的是「{ctx.question.strip()[:40]}」")
    if ctx.goal:
        label = _label_of(GOAL_OPTIONS, ctx.goal) or ctx.goal_note
        parts.append(f"目的是{label}")
    if ctx.self_level:
        label = _label_of(SELF_LEVEL_OPTIONS, ctx.self_level)
        parts.append(f"这块你{label}")
    if ctx.help_type:
        labels = "、".join(_label_of(HELP_TYPE_OPTIONS, h) or h for h in ctx.help_type)
        parts.append(f"希望我{labels}")
    if not parts:
        return "我还不太确定你要什么，先说说这次想解决什么？"
    return "，".join(parts) + "。"


def _label_of(options: tuple[tuple[str, str, str], ...], value: str) -> str:
    for v, label, _ in options:
        if v == value:
            return label
    return ""


def digest(ctx: IntakeContext) -> dict:
    """卡片上的复述摘要（结构化，前端按字段渲染）。"""
    return {
        "book": (ctx.book or {}).get("title", ""),
        "chapter": (ctx.book or {}).get("chapter", ""),
        "progress": (ctx.book or {}).get("progress"),
        "goal": ctx.goal,
        "goal_label": _label_of(GOAL_OPTIONS, ctx.goal),
        "goal_note": ctx.goal_note,
        "self_level": ctx.self_level,
        "self_level_label": _label_of(SELF_LEVEL_OPTIONS, ctx.self_level),
        "question": ctx.question,
        "help_type": list(ctx.help_type),
        "help_type_labels": [_label_of(HELP_TYPE_OPTIONS, h) for h in ctx.help_type],
    }


# ---- 进度推断 ---------------------------------------------------------------


def infer_reading_target(session: Session, *, user_id: str) -> dict:
    """推断"正在读什么、读到哪"。

    两条数据源的能力不同，必须如实标注 `basis`：
    - 内置书：有 `read_progress` 与章节结构 → 精度到百分比
    - 微信读书：**同步接口不返回整本书阅读进度**（只返回划线与想法），
      所以只能按「最近一条划线所属章节」推断到"第几章"

    推断不出来就返回 `basis="none"`——不猜。猜错的进度会一路影响建议的相关性。
    """
    from app.domain.models.book import Book
    from app.domain.models.knowledge_item import KnowledgeItem

    try:
        book = session.scalars(
            select(Book)
            .where(
                Book.user_id == user_id,
                Book.is_deleted.is_(False),
                Book.read_progress > 0.0,
                Book.read_progress < 1.0,
            )
            .order_by(Book.updated_at.desc())
            .limit(1)
        ).first()
    except Exception:  # pragma: no cover - 推断失败不影响主流程
        book = None

    if book is not None:
        return {
            "book": {
                "book_id": book.id,
                "title": book.title,
                "source": "reader",
                "progress": round(float(book.read_progress or 0.0), 4),
                "chapter": _chapter_at(book),
            },
            "basis": "read_progress",
        }

    try:
        item = session.scalars(
            select(KnowledgeItem)
            .where(
                KnowledgeItem.user_id == user_id,
                KnowledgeItem.source == "weread",
                KnowledgeItem.is_deleted.is_(False),
            )
            .order_by(KnowledgeItem.created_at.desc())
            .limit(1)
        ).first()
    except Exception:  # pragma: no cover
        item = None

    if item is not None:
        loc = item.source_locator or {}
        title = str(loc.get("book_title") or "").strip()
        if title:
            return {
                "book": {
                    "book_id": str(loc.get("book_id") or ""),
                    "title": title,
                    "source": "weread",
                    # 微信读书没有全书进度：字段留 None，前端显示"推断"而不是百分比
                    "progress": None,
                    "chapter": str(loc.get("chapter_title") or "").strip(),
                },
                "basis": "weread_highlight",
            }

    return {"book": None, "basis": "none"}


def _chapter_at(book) -> str:
    """按 `current_char` 定位当前章节标题。章节数据缺失时返回空串。"""
    chapters = book.chapters or []
    pos = int(book.current_char or 0)
    for chapter in chapters:
        start = int(chapter.get("char_start") or 0)
        end = int(chapter.get("char_end") or 0)
        if start <= pos < end or (end == 0 and start <= pos):
            return str(chapter.get("title") or "")
    return ""


# ---- 持久化 -----------------------------------------------------------------


def load(session: Session, *, conversation, user_id: str) -> IntakeContext:
    """读会话里的上下文；没有就新建一个（并做一次进度推断）。"""
    ctx = IntakeContext.from_dict(getattr(conversation, "reading_context", None))
    if ctx.book is None and not ctx.question:
        inferred = infer_reading_target(session, user_id=user_id)
        if inferred.get("book"):
            ctx.book = inferred["book"]
            ctx.progress_source = "system"
            ctx.refresh_missing()
    return ctx


def save(session: Session, *, conversation, ctx: IntakeContext) -> None:
    """写回会话。JSON 列必须整体赋值才会落库（见 ConversationRepository）。"""
    conversation.reading_context = ctx.to_dict()
