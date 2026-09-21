"""对话入口的卡片协议：后端与前端之间唯一的呈现契约。

前端只按 `kind` 渲染，不认识任何能力细节；后端换实现、前端换样式，都不需要动对方。
所以这里集中放**所有**卡片构造与配套回复文案，`copilot`（分发）与 `turns`（异步与操作）
都从这里取——两处各写一份文案，迟早会出现「同一个结果在两条路径上说法不一样」。

几个贯穿全篇的约定：

- **卡片里不放正文以外的东西**：标题、摘要等全部来自用户数据，前端一律转义
  （`web/assets/agent.js` 是纯函数渲染，`tests/js/agent_behavior.js` 锁转义）。
- **降级不是失败**：`state="degraded"` 的卡片必须带上「还有哪部分可用」，
  配套回复也不能只说一句失败——用户会因为一句「失败」而不看其实还能用的内容。
- **可操作的卡片带 `key`**：`key` 是这条卡片在会话里的地址，操作回流时前端按它
  原地替换（见 `turns.apply_action`）。不可操作的卡片不带。
"""
from __future__ import annotations

from app.agent.l1_orchestrator import L1Result
from app.agent.l3_orchestrator import L3Brief
from app.agent.l5_orchestrator import L5Result
from app.agent.router import capability_spec

PENDING_KIND = "pending"
FAILED_KIND = "failed"

# 需要「卡片内直接操作」的卡片类型：读取会话时它们的展示状态要按数据库刷新，
# 否则重新打开页面会看到已经处理过的冲突又变回「待处理」——卡片在说谎。
ACTIONABLE_KINDS: frozenset[str] = frozenset({"l2_conflicts", "l5_diagnosis"})


# ---- 通用 ----------------------------------------------------------------


def pending_card(*, turn_id: str, capability: str, label: str, note: str) -> dict:
    """「已经开工、结果稍后」的占位卡。`turn_id` 既是任务幂等键也是这条卡片的地址。"""
    spec = capability_spec(capability)
    return {
        "kind": PENDING_KIND,
        "key": turn_id,
        "turn_id": turn_id,
        "capability": capability,
        "label": label,
        "note": note,
        "href": spec.href if spec else "",
    }


def failed_card(*, turn_id: str, capability: str, note: str) -> dict:
    """执行失败（重试耗尽）时的兜底卡。

    必须有这张卡：否则一条卡在 `pending` 的消息会永远转圈，用户既不知道出了事，
    也没有重试的入口。
    """
    spec = capability_spec(capability)
    return {
        "kind": FAILED_KIND,
        "key": turn_id,
        "turn_id": turn_id,
        "capability": capability,
        "note": note,
        "label": spec.label if spec else capability,
        "href": spec.href if spec else "",
    }


def guide_card(capability: str) -> dict:
    """未接入对话的能力 → 一张把用户送回原页面的引导卡。

    刻意**不**复用能力自己的编排器去「顺手做一下」：那样用户以为是对话在做，
    出了问题也不知道该找哪个页面看结果。说清楚「还没接进来、先去哪儿」更诚实。
    """
    spec = capability_spec(capability)
    label = spec.label if spec else "这个能力"
    href = spec.href if spec else "/"
    note = (
        f"「{label}」还没接进对话，先去原页面用；接进来之后这里可以直接操作。"
        if href
        else f"「{label}」还没接进对话。"
    )
    return {
        "kind": "guide",
        "capability": capability,
        "label": label,
        "href": href,
        "note": note,
    }


def note_empty_card() -> dict:
    return {
        "kind": "note_empty",
        "title": "没看出要记什么",
        "note": "换成「记一下：正文内容 #标签」的写法再试一次。",
    }


# ---- L1 认知挖掘 -----------------------------------------------------------


def _candidates_payload(result: L1Result) -> list[dict]:
    """召回候选原样带出（含已命中项）。

    沿用 L1 端点的既有约定：这是「本次召回的完整视图」，由调用方自行排除已命中的 id。
    只给一条结果时用户无从判断模型是真定位到了、还是随手挑了一条，所以候选必须带出来。
    """
    return [
        {
            "item_id": c.item_id,
            "title": c.title,
            "snippet": c.snippet,
            "score": c.score,
            "channels": list(c.channels),
        }
        for c in result.candidates
    ]


def l1_located_card(result: L1Result) -> dict:
    return {
        "kind": "l1_located",
        "items": [
            {
                "item_id": item.item_id,
                "title": item.title,
                "snippet": item.snippet,
                "read_progress": round(float(item.read_progress or 0.0), 4),
                "embed_status": item.embed_status,
                "href": f"/knowledge.html?item={item.item_id}",
            }
            for item in result.located_items
        ],
        "candidates": _candidates_payload(result),
        "hint": result.read_hint,
        "reason": result.reason,
    }


def l1_clarify_card(result: L1Result) -> dict:
    return {
        "kind": "l1_clarify",
        "question": result.question,
        "candidates": _candidates_payload(result),
        "turn": result.turn,
        "max_turns": result.max_turns,
        "reason": result.reason,
    }


def l1_empty_card() -> dict:
    return {
        "kind": "l1_empty",
        "title": "知识库还是空的",
        "note": "先录入几条知识再来挖掘——没有素材就没法按线索找回。",
        "href": "/knowledge.html",
    }


def located_reply(result: L1Result) -> str:
    """命中后的那一句回复。标题列出来，细节交给卡片。"""
    if not result.located_items:
        return "没能定位到具体条目，换个说法再试试？"
    titles = "、".join(item.title for item in result.located_items[:3])
    reply = f"找到 {len(result.located_items)} 条：{titles}"
    if result.read_hint:
        reply = f"{reply}。{result.read_hint}"
    return reply


# ---- L2 冲突检测 -----------------------------------------------------------


def l2_conflicts_card(*, key: str, items: list[dict], summary: dict) -> dict:
    """冲突卡组。

    `items[].user_state` 是**会变的**：读取会话时由 `turns` 按数据库刷新。
    前端据此决定每张冲突卡上还能点哪些按钮（已处理的就只剩「标回待处理」）。
    """
    spec = capability_spec("l2")
    return {
        "kind": "l2_conflicts",
        "key": key,
        "items": items,
        "summary": summary,
        "href": spec.href if spec else "",
    }


def scan_reply(summary: dict) -> str:
    """扫描后的那一句回复。数字全部来自本地统计，模型不参与。"""
    found = int(summary.get("conflicts_found") or 0)
    scanned = int(summary.get("scanned_items") or 0)
    judged = int(summary.get("pairs_judged") or 0)
    parts = [f"扫了 {scanned} 条、判了 {judged} 对主张"]
    if found:
        parts.append(f"发现 {found} 处矛盾")
    else:
        parts.append("没有发现新的矛盾")
    suppressed = int(summary.get("conflicts_suppressed") or 0)
    if suppressed:
        parts.append(f"另有 {suppressed} 处因你反复忽略的同类冲突被收敛")
    failures = int(summary.get("extraction_failures") or 0)
    if failures:
        parts.append(f"{failures} 条条目主张提取失败，下次扫描会自动重试")
    return "，".join(parts) + "。"


# ---- L3 认知助产 -----------------------------------------------------------


def l3_brief_card(result: L3Brief) -> dict:
    """L3 认知简报卡。

    `state` 一并带出：`degraded` 时主题分布仍有效、只是追问生成失败，前端要能
    **保留已有的部分**并把降级原因说清楚，而不是整张卡当成失败。
    """
    spec = capability_spec("l3")
    return {
        "kind": "l3_brief",
        "state": result.state,                 # ok | empty | degraded
        "analyzed_items": result.analyzed_items,
        "topics": [
            {"topic": stat.topic, "count": stat.count, "levels": dict(stat.levels)}
            for stat in result.topics
        ],
        "patterns": list(result.patterns),
        "questions": [
            {
                "question": q.question,
                "why": q.why,
                "evidence": q.evidence,
                "next_step": q.next_step,
            }
            for q in result.questions
        ],
        "conflicts": dict(result.conflict_stats),
        "overview": result.overview,
        "note": result.note,
        "href": spec.href if spec else "",
    }


def brief_reply(result: L3Brief) -> str:
    """简报那一句回复。

    降级时**不要**说「失败」：`degraded` 表示主题分布仍然有效、只是追问那一步没成，
    卡片里还有可用内容，一句「失败了」会让用户直接放弃看。
    """
    if result.state == "ok":
        return (
            f"分析了 {result.analyzed_items} 条，"
            f"{len(result.topics)} 个主题，{len(result.questions)} 个值得想的问题"
        )
    return result.note or "简报没能完整生成，稍后再试一次。"


# ---- L5 归因诊断 -----------------------------------------------------------


def l5_diagnosis_card(*, key: str, result: L5Result) -> dict:
    spec = capability_spec("l5")
    metrics = result.metrics
    return {
        "kind": "l5_diagnosis",
        "key": key,
        "state": result.state,
        "diagnosis_id": result.diagnosis_id,
        "pattern": result.pattern,
        "root_cause": result.root_cause,
        "confidence": result.confidence,
        "suggested_action": result.suggested_action,
        "reasoning_chain": list(result.reasoning_chain),
        "status": "pending",          # 由读取路径按数据库刷新（pending/accepted/rejected）
        "metrics": {
            "total_items": metrics.total_items,
            "completed_items": metrics.completed_items,
            "completion_ratio": round(float(metrics.completion_ratio or 0.0), 4),
            "study_events_week": metrics.study_events_week,
            "idle_days": metrics.idle_days,
            "unseen_conflicts": metrics.unseen_conflicts,
        } if metrics else {},
        "note": result.note,
        "href": spec.href if spec else "",
    }


def diagnosis_reply(result: L5Result) -> str:
    if result.state == "ok":
        return f"诊断完成：「{result.pattern}」，置信度 {round(result.confidence * 100)}%"
    return result.note or "这次没能得出诊断结论。"


# ---- 知识录入 --------------------------------------------------------------


def knowledge_created_card(item) -> dict:
    return {
        "kind": "knowledge_created",
        "item_id": item.id,
        "title": item.title,
        "tags": [str(tag) for tag in (item.tags or [])],
        "embed_status": item.embed_status,
        "href": f"/knowledge.html?item={item.id}",
    }
