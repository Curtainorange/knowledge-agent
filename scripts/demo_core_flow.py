"""核心链路端到端演示脚本（G 模块 AT-G3）。

## 它解决什么问题

`pytest` 证明「逻辑对」，`smoke_modules.py` 证明「模型产出能用」，两者都**不证明
用户路径能走通**——卡片之间的衔接、异步轮询、槽位补齐、卡片被否掉之后的状态，
只有把整条链路当成用户走一遍才看得见。此前验收靠人肉点页面，漏点一步就没人知道。

本脚本按 10-20 方案 G 模块的 5 步演示跑：**连续跑 N 次（默认 2 次）全通才允许上线**。

| 步 | 演示什么 | 判据 |
|---|---|---|
| 1 | 开场探明意图 | 开场卡是 `reading_intake`，且问的是槽位而不是直接推动作 |
| 2 | 给建议 | 补槽 → 生成 → `suggestion` 卡 state=ok，每条建议的依据都在证据池里 |
| 3 | 效果回访 | 「我去做了」→「有用了」两步都落到卡片上，并写回会话 |
| 4 | 冲突归因 | 七项结构化字段齐全，背景非空且不超长（≤120 字硬上限） |
| 5 | 计划依据 | `rules_snapshot` 6 项齐全，单周时长 ≤ 周容量（AT-F2 硬断言） |

## 两种模式

- `--mode mock`（默认）：清空两个 KEY，走 `MockProvider`。几秒跑完、不花钱，
  适合每次改动后跑和 CI。
- `--mode real`：用 `.env` 里的真实 KEY。会真的花 API 费用（约 30s / 轮），
  只在演示前或改了提示词之后跑。

两者**断言完全相同**——差别只有模型来源。这正是「Mock 测不出参数错」的教训之后的
补法：参数类断言（阈值、容量、字段齐不齐）在 mock 下也必须成立。

## 不碰 dev.db

用临时 SQLite 文件（跑完删除，`--keep-db` 可保留排查）。用户库里有真实笔记，
演示脚本不该往里写测试数据。

用法：
    python scripts/demo_core_flow.py                 # mock，跑 2 次
    python scripts/demo_core_flow.py --mode real     # 真实模型
    python scripts/demo_core_flow.py --repeat 1 --keep-db

退出码：0 全通；1 有步骤失败；2 环境不满足（real 模式缺 KEY）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

DB_PATH = Path(tempfile.gettempdir()) / "cogcop_demo_flow.db"

# ---- 环境准备必须在 import app.* 之前完成 ------------------------------------
# engine 在 `app.db` 导入时按 settings.database_url 建好，事后再改 settings 无效。
from app.core.config import settings  # noqa: E402

_mode = "mock"          # 由 main() 覆盖；模块级先给默认值，便于被 import 时也安全
_repeat = 2

settings.database_url = f"sqlite:///{DB_PATH.as_posix()}"
settings.worker_enabled = True            # 异步能力靠 worker 跑，演示必须真跑
settings.l2_realtime_trigger_enabled = False  # 造数据时会触发扫描，演示里用不上，关掉免得噪音
# 周扫必须关：worker 一启动就会 ensure 本周扫描，L2 重扫会**替换掉脚本手工造的主张**
# （`_needs_rescan` 看到 claims_scanned_at 为空就重扫），冲突随即指向已删除的主张，
# 第 4 步归因拿不到双方原文 → skipped。只发生在真跑 worker 的 real 模式，
# mock 模式（worker 关）测不出来——这类「环境差异造成的假故障」正是脚本要吃掉的。
settings.l2_weekly_scan_enabled = False
settings.push_schedule_enabled = False    # 演示不需要定时推送

# 轮询上限：mock 秒回，real 有模型延迟
WAIT_MOCK = 90.0
WAIT_REAL = 300.0

# 自建的 engine 都要登记：SQLite 文件被连接占着时删不掉，
# 而「删不掉」在 Windows 上只报一句不相干的 WinError 32（smoke_modules 踩过）。
_ENGINES: list = []


def _engine():
    from sqlalchemy import create_engine

    eng = create_engine(f"sqlite:///{DB_PATH.as_posix()}")
    _ENGINES.append(eng)
    return eng


class DemoError(AssertionError):
    """某一步没走通。消息直接给人看，要写清「看到什么 / 期望什么」。"""


# ---- mock 模式的假模型 -------------------------------------------------------
#
# 为什么不用现成的 MockProvider：它是**纯回声**（把 prompt 原样拼一段前缀返回），
# 只能证明「调用链没断」，证明不了「返回的东西能进卡片」。而本脚本的断言全在
# 卡片上（依据是否可溯源、七项齐不齐、周容量超没超），所以需要一个按 task_type
# 回放**合规 JSON** 的假供应商。
#
# 关键一点：建议的 `basis_item_ids` 从 prompt 里**实际出现的证据 id** 里取，
# 不写死。写死就成了「假引用」，引用校验（verify_citations）反而测不出来。


def _evidence_ids_in(messages: list[dict]) -> list[str]:
    text = "\n".join(str(m.get("content") or "") for m in messages)
    return re.findall(r"\[(ki:[0-9a-zA-Z\-]+)\]", text)


def _demo_text(task_type: str, messages: list[dict]) -> str:
    if task_type == "reading_suggestion":
        ids = _evidence_ids_in(messages)[:2]
        return json.dumps({
            "items": [
                {
                    "action": "下一次周会只改一件事：先让每人对齐一个「我不确定的判断」",
                    "why": "你对极度透明的关注点在信息可见性，而不是决策权分配。",
                    "steps": ["周会开场花 5 分钟", "每人说一个不确定的判断", "只记录不评价"],
                    "how_to_verify": "数一遍这次周会里有多少条是在补充信息，而不是在表态",
                    "basis_item_ids": ids[:1],
                },
                {
                    "action": "给自己加一条反向检查：这句话公开后，会不会有人不敢试错",
                    "why": "同一主题还有另一种立场：过度透明会抑制试错。",
                    "steps": ["每次公开前问一遍", "连续两周记录被压下的想法数"],
                    "how_to_verify": "看两周内被自己压下的想法条数是增加还是减少",
                    "basis_item_ids": ids[1:2] or ids[:1],
                },
            ],
            "reason": "",
        }, ensure_ascii=False)
    if task_type == "conflict_attribution":
        return json.dumps({
            "cause_type": "适用边界",
            "background_a": "作者从组织信息流动的角度谈透明，关注的是问题能不能被看见。",
            "background_b": "作者从团队心理安全的角度谈透明，关注的是公开之后还敢不敢试错。",
            "premise_a": "前提是团队已经有说真话的安全感。",
            "premise_b": "前提是公开范围包含未完成的想法。",
            "boundary_a": "适用于信息堵在少数人手里、问题迟迟暴露不出来的团队。",
            "boundary_b": "适用于新人多、试错成本高、公开即定论的环境。",
            "scene_guidance": [
                {"scene": "问题反复出现却没人提", "follow": "a", "reason": "先把信息放出来"},
                {"scene": "团队不敢做没把握的事", "follow": "b", "reason": "先缩小公开范围"},
            ],
            "probe": {
                "question": "下一次周会里，补充信息的发言和表态的发言各占多少？",
                "how_to_check": "数一遍两类发言的条数，比例低于 3:7 说明透明还没起作用",
            },
        }, ensure_ascii=False)
    if task_type == "plan_generation":
        return json.dumps({
            "tasks": [
                {"week_index": 1, "subject": "读透极度透明的前提条件",
                 "focus": "找出「安全感」在原文里的定义", "related_item_ids": []},
                {"week_index": 2, "subject": "在自己的周会上试一次",
                 "focus": "只改开场 5 分钟", "related_item_ids": []},
                {"week_index": 3, "subject": "复盘试的那一次",
                 "focus": "对照原文判断哪里走样", "related_item_ids": []},
            ],
            "rationale": "先读懂前提，再小范围试，最后复盘。",
        }, ensure_ascii=False)
    return json.dumps({}, ensure_ascii=False)


class DemoProvider:
    """按 task_type 回放合规 JSON 的假供应商（仅 mock 模式使用）。"""

    is_mock = True

    def chat(self, *, model, reasoning, messages, tools=None, response_format=None,
             task_type="default"):
        from app.llm.completion import Completion

        text = _demo_text(task_type, messages)
        return Completion(
            text=text, prompt_tokens=1, completion_tokens=1, finish_reason="stop",
            model=model or "demo", reasoning=reasoning,
        )


# ---- 工具函数 ----------------------------------------------------------------


def _log(text: str = "") -> None:
    print(text)


def _step(index: int, title: str) -> None:
    print(f"  [{index}/5] {title}")


def _fail(message: str) -> None:
    raise DemoError(message)


def _latest_card(client, headers: dict, conversation_id: str, kind: str) -> dict | None:
    data = client.get(
        f"/api/v1/agent/conversation/{conversation_id}", headers=headers
    ).json()
    cards = [
        m.get("card") for m in data.get("messages", [])
        if isinstance(m.get("card"), dict) and m["card"].get("kind") == kind
    ]
    return cards[-1] if cards else None


def _wait_card(
    client, headers: dict, conversation_id: str, kind: str, *, timeout: float
) -> dict:
    """等一张**终结态**卡片（state 不再是 pending / running）。"""
    deadline = time.monotonic() + timeout
    last_state = "（还没出现）"
    while time.monotonic() < deadline:
        card = _latest_card(client, headers, conversation_id, kind)
        if card is not None:
            last_state = str(card.get("state") or "")
            if last_state not in ("pending", "running"):
                return card
        time.sleep(0.4)
    _fail(f"等 {kind} 卡超时（{timeout:.0f}s），最后状态：{last_state}")


def _chat(client, headers: dict, conversation_id: str, message: str) -> dict:
    resp = client.post(
        "/api/v1/agent/chat",
        json={"conversation_id": conversation_id, "message": message},
        headers=headers,
    )
    if resp.status_code != 200:
        _fail(f"发消息失败 {resp.status_code}：{resp.text[:200]}")
    return resp.json()


def _action(client, headers: dict, conversation_id: str, card: dict, action: str,
            *, target_id: str = "", value: str = "") -> dict:
    resp = client.post(
        "/api/v1/agent/actions",
        json={
            "conversation_id": conversation_id,
            "card_key": card.get("key") or "",
            "action": action,
            "target_id": target_id,
            "value": value,
        },
        headers=headers,
    )
    if resp.status_code != 200:
        _fail(f"卡片操作 {action} 失败 {resp.status_code}：{resp.text[:200]}")
    return resp.json()


# ---- 造演示数据（直连，与脚本共用同一个临时库）-------------------------------


def _seed_knowledge(session, user_id: str) -> list[str]:
    """两条同主题的笔记：一条够建议召回，两条够造冲突。"""
    from app.ingestion.service import IngestionService
    from app.retrieval.embedding import build_embedding

    embedding = build_embedding()
    svc = IngestionService(session, embedding)
    a = svc.add_knowledge(
        user_id=user_id,
        title="原则 · 极度透明",
        content=(
            "极度透明意味着组织里没有不能说的话。它讲的是信息在组织内部的可见性，"
            "不是决策权的分配。要让极度透明真正起作用，团队需要先建立说真话的安全感，"
            "否则它只会变成一句口号。落到周会上，可以先让每个人说出一个自己不确定的判断。"
        ),
    )
    b = svc.add_knowledge(
        user_id=user_id,
        title="团队协作的暗面",
        content=(
            "过度透明会让人不敢试错：一旦所有半成品都被公开，成员会倾向于只做安全的决定。"
            "更稳妥的做法是先在小范围里透明，等信任建立再扩大范围。"
        ),
    )
    session.commit()
    return [a.id, b.id]


def _seed_conflict(session, user_id: str, item_ids: list[str]) -> str:
    """造一条已入库的冲突，供第 4 步归因用。

    不跑 L2 扫描：演示验的是**归因**这一步，扫描会把模型调用和幂等搅进来，
    失败原因就分不清是归因坏了还是判定坏了。
    """
    from app.domain.repositories.claim_repository import ClaimRepository
    from app.domain.repositories.conflict_repository import ConflictRepository

    crepo = ClaimRepository(session, user_id=user_id)
    claim_a = crepo.create(
        user_id=user_id, knowledge_item_id=item_ids[0],
        statement="极度透明能让团队更快发现问题", topic="团队透明",
    )
    claim_b = crepo.create(
        user_id=user_id, knowledge_item_id=item_ids[1],
        statement="过度透明会让人不敢试错", topic="团队透明",
    )
    conflict = ConflictRepository(session, user_id=user_id).create(
        user_id=user_id,
        item_a_id=item_ids[0], item_b_id=item_ids[1],
        claim_a_id=claim_a.id, claim_b_id=claim_b.id,
        conflict_type="适用边界", confidence=0.85,
    )
    session.commit()
    return conflict.id


# ---- 五步 --------------------------------------------------------------------


def step1_opening(client, headers: dict) -> tuple[str, dict]:
    _step(1, "开场探明意图")
    resp = client.post("/api/v1/agent/start", json={}, headers=headers)
    if resp.status_code != 200:
        _fail(f"开场失败 {resp.status_code}：{resp.text[:200]}")
    data = resp.json()
    conversation_id = data["conversation_id"]

    cards = [m.get("card") for m in data.get("messages", []) if isinstance(m.get("card"), dict)]
    if not cards:
        _fail("开场没有任何卡片——B 模块要求先问一句，而不是空口问候")
    card = cards[-1]
    if card.get("kind") != "reading_intake":
        _fail(f"开场卡是 {card.get('kind')}，期望 reading_intake（先确认再动手）")
    if not card.get("questions"):
        _fail("开场卡没有问题项——用户无从作答，确认流程等于没做")
    _log(f"      开场卡：{card.get('kind')}，问 {len(card['questions'])} 个槽位")
    return conversation_id, card


def step2_suggestion(
    client, headers: dict, conversation_id: str, card: dict, *, timeout: float
) -> dict:
    """补槽 → 生成 → 校验引用。

    **始终以接口返回的卡片为准**，不从会话里重读：会话里同 key 的卡片可能有多张
    （开场一张、澄清态下每说一句话就追加一张），`_replace_card` 只换第一张，
    取最后一张就会拿到旧状态——脚本会一直在同一个槽位上打转（踩过一次）。
    """
    _step(2, "给建议（补槽 → 生成 → 引用可溯源）")

    # 前端点按钮走的就是这条路径：每题选第一个选项，直到卡片给出「生成」按钮。
    for round_index in range(8):
        if card.get("kind") != "reading_intake":
            break
        if card.get("sends"):
            phrase = card["sends"][0]["message"]
            _log(f"      槽位已齐（补了 {round_index} 轮），发「{phrase}」")
            turn = _chat(client, headers, conversation_id, phrase)
            _log(f"      分流：{turn.get('capability')}（{turn.get('decided_by') or '?'}）")
            break
        questions = card.get("questions") or []
        if not questions:
            _fail("intake 卡既没有 sends 也没有 questions——用户卡在中间无路可走")
        q = questions[0]
        options = q.get("options") or []
        if not options:
            # 自由文本槽位（「想解决什么」）：前端给的是对话输入框而不是按钮，
            # 这里也走说话——顺带验证「澄清态下用户说的话会被当成回答」。
            answer = "我想搞清楚极度透明在自己团队的周会上到底怎么落地"
            turn = _chat(client, headers, conversation_id, answer)
            _log(f"      答 {q.get('slot')}（自由文本）：{answer[:16]}…")
            card = turn.get("card") or {}
            continue
        result = _action(
            client, headers, conversation_id, card, "intake.answer",
            target_id=str(q.get("slot") or ""), value=str(options[0]["value"]),
        )
        _log(f"      答 {q.get('slot')} = {options[0]['value']}")
        card = result.get("card") or {}
    else:
        _fail("补槽 8 轮仍未齐——槽位收敛不了，演示走不下去")

    card = _wait_card(client, headers, conversation_id, "suggestion", timeout=timeout)
    state = card.get("state")
    if state == "insufficient_evidence":
        _fail(f"库里已塞了相关内容却判依据不足：{card.get('note')}（查阈值与嵌入模型）")
    if state != "ok":
        _fail(f"建议卡 state={state}：{card.get('note')}")

    items = card.get("items") or []
    if not items:
        _fail("state=ok 却没有建议项")
    pool = {ev.get("evidence_id") for ev in card.get("evidence") or []}
    for index, item in enumerate(items):
        if not item.get("how_to_verify"):
            _fail(f"第 {index + 1} 条建议没有 how_to_verify——无法验证的建议等于意见")
        stray = set(item.get("basis_item_ids") or []) - pool
        if stray:
            _fail(f"第 {index + 1} 条引用了证据池外的依据：{stray}")
    library_level = [
        ev for ev in card.get("evidence") or []
        if str(ev.get("basis") or "") == "library"
    ]
    if not library_level:
        _fail("没有一条 library 级依据——B 模块要求至少一条来自知识库本身")
    _log(f"      建议 {len(items)} 条，证据 {len(pool)} 条（library 级 {len(library_level)} 条）")
    return card


def step3_followup(client, headers: dict, conversation_id: str, card: dict) -> None:
    """效果回访：做了吗 → 有用吗，并把状态写回会话。

    这一步之前是缺的（只有「这条不对」），所以在这儿卡住过一次——
    演示脚本的价值就在这：卡片上少一条腿，走一遍就露出来了。
    """
    _step(3, "效果回访（做了 → 有用 → 写回会话）")
    items = card.get("items") or []
    if not items:
        _fail("没有可回访的建议项")
    target = str(items[0].get("id") or "")

    done = _action(client, headers, conversation_id, card, "suggestion.done", target_id=target)
    done_card = done.get("card") or {}
    first = (done_card.get("items") or [{}])[0]
    if first.get("status") != "done":
        _fail(f"点了「我去做了」但条目状态是 {first.get('status')!r}，期望 done")
    _log("      已标记「我去做了」")

    scored = _action(
        client, headers, conversation_id, done_card,
        "suggestion.effect", target_id=target, value="worked",
    )
    scored_card = scored.get("card") or {}
    first = (scored_card.get("items") or [{}])[0]
    if first.get("effect") != "worked":
        _fail(f"效果没记上：{first.get('effect')!r}")
    _log("      效果记为「有用了」")

    # 第二条仍然可以否掉——两种反馈互不干扰，否则「有用」就成了唯一出口
    rest = scored_card.get("items") or []
    if len(rest) > 1:
        rejected = _action(
            client, headers, conversation_id, scored_card,
            "suggestion.reject", target_id=str(rest[1].get("id") or ""),
        )
        after = (rejected.get("card") or {}).get("items") or []
        if len(after) != len(rest) - 1:
            _fail(f"否掉一条后剩 {len(after)} 条，期望 {len(rest) - 1} 条")
        _log(f"      另否掉一条，剩 {len(after)} 条")

    # 写回会话是关键：只改前端内存的话，刷新一次就回到没回访过的状态
    stored = _latest_card(client, headers, conversation_id, "suggestion") or {}
    stored_item = (stored.get("items") or [{}])[0]
    if stored_item.get("effect") != "worked":
        _fail(
            f"回访状态没写回会话：库里 effect={stored_item.get('effect')!r}"
            "（只改了接口返回的卡，刷新就会回退）"
        )
    _log("      状态已写回会话")


def step4_attribution(client, headers: dict, conflict_id: str) -> None:
    _step(4, "冲突归因（七项结构化字段）")
    resp = client.post(
        f"/api/v1/l2/conflicts/{conflict_id}/attribute", headers=headers
    )
    if resp.status_code != 200:
        _fail(f"归因失败 {resp.status_code}：{resp.text[:240]}")
    data = resp.json()
    if data.get("state") not in ("ok", "already"):
        _fail(f"归因 state={data.get('state')}：{data.get('note')}")

    required = ("cause_type", "background_a", "background_b",
                "premise_a", "premise_b", "boundary_a", "boundary_b")
    missing = [key for key in required if not str(data.get(key) or "").strip()]
    if missing:
        _fail(f"归因缺字段：{missing}（背景是必填，缺了只能退化成「看法不同」）")
    for key in ("background_a", "background_b"):
        length = len(data[key])
        if length > 120:
            _fail(f"{key} 长 {length} 字，超过 120 字硬上限（提示词要求一句话）")

    scenes = data.get("scene_guidance") or []
    if not scenes:
        _fail("没有场景指引——用户还是不知道「什么时候听谁的」")
    probe = data.get("probe") or {}
    if not str(probe.get("question") or "").strip():
        _fail("没有可验证的判据")
    _log(f"      原因类型：{data['cause_type']}")
    _log(f"      背景A：{data['background_a'][:48]}")
    _log(f"      场景 {len(scenes)} 条，判据：{str(probe.get('question') or '')[:40]}")


def step5_plan(client, headers: dict, conversation_id: str, *, timeout: float) -> None:
    _step(5, "计划依据（规则透明 + 周容量硬约束）")
    resp = client.post(
        "/api/v1/l4/goals",
        json={"description": "两个月内把极度透明用在自己的周会上", "priority": "high"},
        headers=headers,
    )
    if resp.status_code not in (200, 201):
        _fail(f"建目标失败 {resp.status_code}：{resp.text[:200]}")

    _chat(client, headers, conversation_id, "生成周计划")
    card = _wait_card(client, headers, conversation_id, "l4_plan", timeout=timeout)
    if card.get("state") != "ok":
        _fail(f"计划卡 state={card.get('state')}：{card.get('note')}")

    snapshot = card.get("rules_snapshot") or {}
    needed = ("version", "weekly_minutes", "new_ratio",
              "review_weeks", "min_task_minutes", "max_task_minutes")
    missing = [key for key in needed if key not in snapshot]
    if missing:
        _fail(f"rules_snapshot 缺项：{missing}（缺项即 AT-F1 fail）")

    tasks = card.get("tasks") or []
    if not tasks:
        _fail("计划没有任务")
    weekly = int(snapshot["weekly_minutes"])
    load: dict[int, int] = {}
    for task in tasks:
        load[int(task.get("week_index") or 0)] = load.get(int(task.get("week_index") or 0), 0) + int(
            task.get("minutes") or 0
        )
    over = {week: minutes for week, minutes in load.items() if minutes > weekly}
    if over:
        _fail(f"单周时长超容量（上限 {weekly} 分钟）：{over}（AT-F2 是硬断言）")

    no_basis = [
        task for task in tasks
        if not str((task.get("basis") or {}).get("rule") or "").strip()
    ]
    if no_basis:
        _fail(f"{len(no_basis)} 条任务没有规则依据（AT-F4）")
    _log(f"      任务 {len(tasks)} 条，{len(load)} 周，最重一周 {max(load.values())}/{weekly} 分钟")
    _log(f"      规则版本 {snapshot['version']}，复习周 {snapshot['review_weeks']}")


# ---- 单轮 / 主流程 -----------------------------------------------------------


def run_round(index: int, *, timeout: float) -> int:
    """跑一轮五步；返回失败步数（0 = 全通）。"""
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import sessionmaker

    import app.domain.models  # noqa: F401  触发全部模型注册
    from app.domain.models.base import Base
    from app.main import app

    engine = _engine()
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    print(f"\n=== 第 {index} 轮（用户 demo_{index}）===")
    failed = 0
    from app.api import deps as api_deps
    from app.llm.gateway import ModelGateway

    if _mode == "mock":
        # 注入假供应商：网关依赖被替换后，**同步路径**（worker 关闭时）走的就是它。
        app.dependency_overrides[api_deps.get_gateway] = lambda: ModelGateway(provider=DemoProvider())
    with TestClient(app) as client:
        username = f"demo_{index}"
        password = "demo-pass-1234"
        reg = client.post(
            "/api/v1/auth/register",
            json={"username": username, "password": password, "nickname": "演示"},
        )
        if reg.status_code not in (200, 201):
            print(f"  ✗ 注册失败 {reg.status_code}：{reg.text[:200]}")
            return 1
        token = reg.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        user_id = reg.json()["user_id"]

        session = Session()
        try:
            item_ids = _seed_knowledge(session, user_id)
            conflict_id = _seed_conflict(session, user_id, item_ids)
        finally:
            session.close()

        conversation_id = ""
        try:
            conversation_id, card = step1_opening(client, headers)
            card = step2_suggestion(client, headers, conversation_id, card, timeout=timeout)
            step3_followup(client, headers, conversation_id, card)
            step4_attribution(client, headers, conflict_id)
            step5_plan(client, headers, conversation_id, timeout=timeout)
        except DemoError as exc:
            failed += 1
            print(f"  ✗ {exc}")
        except Exception as exc:  # noqa: BLE001 - 演示脚本要报清楚，不要吞栈
            failed += 1
            print(f"  ✗ 异常 {type(exc).__name__}: {exc}")
            traceback.print_exc(limit=4)
    if _mode == "mock":
        app.dependency_overrides.pop(api_deps.get_gateway, None)
    return failed


def main(argv: list[str] | None = None) -> int:
    global _mode, _repeat

    parser = argparse.ArgumentParser(description="核心链路端到端演示（G 模块 AT-G3）")
    parser.add_argument("--mode", choices=("mock", "real"), default="mock",
                        help="mock 走 MockProvider（默认，不花钱）；real 用 .env 的真实 KEY")
    parser.add_argument("--repeat", type=int, default=2,
                        help="连续跑几轮（G 模块要求 ≥2，默认 2）")
    parser.add_argument("--keep-db", action="store_true", help="保留临时库便于排查")
    args = parser.parse_args(argv)

    _mode = args.mode
    _repeat = max(1, args.repeat)

    if _mode == "real":
        if not settings.deepseek_api_key:
            print("✗ 没有读到 DEEPSEEK_API_KEY，先检查 .env（或用 --mode mock）")
            return 2
        print(f"模型链：DeepSeek（key 长度 {len(settings.deepseek_api_key)}）")
        settings.worker_enabled = True
        timeout = WAIT_REAL
    else:
        # mock 走**同步路径**：worker 里的 handler 自建 `ModelGateway()`
        # （`app/workers/handlers.py`），拿不到依赖注入的假供应商，关掉 worker
        # 才会走 `execute_capability` 的同步兜底——那也是真实的降级路径。
        settings.deepseek_api_key = ""
        settings.mimo_api_key = ""
        settings.embedding_backend = "hash"
        settings.worker_enabled = False
        print("模型链：DemoProvider（按 task_type 回放合规 JSON，不触网）")
        timeout = WAIT_MOCK

    print(f"临时库：{DB_PATH.name}    轮数：{_repeat}")

    if DB_PATH.exists():
        DB_PATH.unlink()

    total_failed = 0
    started = time.monotonic()
    for index in range(1, _repeat + 1):
        total_failed += run_round(index, timeout=timeout)
    elapsed = time.monotonic() - started

    # 必须先把 app 的 engine 与所有自建连接池释放再删文件：
    # 文件被连接占着时，删除会以完全不相干的说法失败（smoke_modules 踩过）。
    try:
        from app.domain import db as app_db

        app_db.engine.dispose()
    except Exception as exc:  # pragma: no cover - 清理失败不影响结论
        print(f"（应用 engine 释放失败：{exc}）")
    for eng in _ENGINES:
        try:
            eng.dispose()
        except Exception:  # pragma: no cover
            pass
    _ENGINES.clear()
    if not args.keep_db and DB_PATH.exists():
        try:
            DB_PATH.unlink()
        except OSError as exc:
            print(f"（临时库未能自动删除：{exc}，可手动删 {DB_PATH}）")
    elif args.keep_db:
        print(f"\n临时库已保留：{DB_PATH}")

    print("\n" + "=" * 64)
    if total_failed:
        print(f"✗ {total_failed} 轮未走通（共 {_repeat} 轮，{elapsed:.1f}s）")
        return 1
    print(f"✓ {_repeat} 轮 × 5 步全部走通（{elapsed:.1f}s）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
