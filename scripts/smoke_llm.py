"""LLM 供应商连通性冒烟：不碰业务库，只验证「模型层能不能用」。

用法（项目根目录，KEY 从 .env 读取）：
    python scripts/smoke_llm.py

看六件事，对应本项目对模型层的全部依赖：
0. 配置自检——供应商链（主/备）、回退与 JSON 修复开关是否如预期；
1. 基础对话——验证 key / base_url / 模型名是否可用；
2. 思考模式——**直调 Provider**（reasoning 由网关的策略表决定，不是 chat 的参数），
   验证供应商的思考开关字段真的生效（DeepSeek 用 `reasoning`；MiMo 用 `thinking.type`）；
3. function calling——验证工具调用回包能被基类解析成 `[tool_request] ...`
   （P0 不执行工具，但分流/工具白名单依赖这条链路不报错）；
4. JSON 任务经网关——验证「期望严格 JSON」的任务被自动带上原生 JSON 约束且回包可解析；
5. JSON 约束注入逻辑（不花 token）——直接看网关算出来的 response_format。

为什么单独有这个脚本：`smoke_l2/l3/agent` 都会顺带打真实模型，但一个失败要跑很久
才看得出来是「模型层坏了」还是「业务逻辑坏了」。这个脚本几次调用即定位到层。

非零退出码 = 至少有一项失败（便于 CI / 手工复检）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.llm.gateway import ModelGateway, _build_provider  # noqa: E402

# 期望每个供应商用的思考开关字段名
EXPECTED_THINKING_KEY = {"deepseek": "reasoning", "mimo": "thinking"}


def main() -> int:
    ok = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        print(f"[{'OK  ' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))
        if not cond:
            ok = False

    provider = settings.model_provider
    model = settings.active_model
    print(f"供应商：{provider}    模型：{model}\n")

    if provider == "mock":
        print("[FAIL] 未配置 MIMO_API_KEY / DEEPSEEK_API_KEY，当前为 MockProvider，无法验证真实连通性")
        return 1

    gw = ModelGateway()
    real = _build_provider()

    # ---- 0) 配置自检：供应商链 / 回退 / 修复回合（不花 token）----------------
    from app.llm.gateway import _build_provider_chain  # noqa: E402

    chain = _build_provider_chain()
    names = " → ".join(type(p).__name__ for p in chain)
    print(f"→ 0/5 配置自检\n   供应商链：{names}")
    print(f"   回退开关：{'开' if settings.llm_fallback_enabled else '关'}"
          f"    JSON 修复回合：{'开' if settings.llm_json_repair_enabled else '关'}")
    check("链首即当前启用的供应商", type(chain[0]).__name__.lower().startswith(provider),
          type(chain[0]).__name__)
    if len(chain) > 1:
        check("备用供应商已就位（主供应商失败可自动切换）", True, type(chain[1]).__name__)
    else:
        print("   [INFO] 链上只有一个供应商：主供应商失败时不会自动切换"
              "（另一个 KEY 未配置，或 LLM_FALLBACK_ENABLED=false）")
    check("JSON 修复回合已开启", settings.llm_json_repair_enabled,
          "关掉后结构化输出失败就直接降级，不会多试一次")

    # ---- 5) JSON 约束注入逻辑（先看，不花 token）----------------------------
    print("→ JSON 约束注入（不消耗 token）")
    key = EXPECTED_THINKING_KEY.get(provider)
    extra_on = real._thinking_extra_body(True)
    extra_off = real._thinking_extra_body(False)
    print(f"   思考开关字段：on={extra_on}  off={extra_off}")
    check(f"{provider} 思考开关字段为 `{key}`", bool(key and extra_on and key in extra_on))
    if provider == "mimo":
        check("MiMo 思考默认开，off 时显式 disabled", bool(extra_off))
    else:
        check("DeepSeek 思考默认关，off 时不注入", extra_off is None)

    # 无 schema 时退到 json_object；纯对话任务不带
    bare = gw._effective_response_format("topic_analysis", None, None)
    chat_fmt = gw._effective_response_format("multi_turn_dialogue", None, None)
    print(f"   topic_analysis（无 schema）→ {bare}")
    print(f"   multi_turn_dialogue        → {chat_fmt}")
    if provider == "mimo":
        check("JSON 任务被注入 json_object", bare == {"type": "json_object"}, str(bare))
    check("对话任务不注入（保持自由文本）", chat_fmt is None, str(chat_fmt))

    # 带 json_model 时应派生 json_schema（结构一起约束）
    from pydantic import BaseModel

    class _Probe(BaseModel):
        topic: str
        count: int

    schema_fmt = gw._effective_response_format("topic_analysis", None, _Probe)
    if provider == "mimo":
        check("带 json_model 时注入 json_schema", bool(schema_fmt and schema_fmt.get("type") == "json_schema"),
              str(schema_fmt)[:120])
        check("schema 由 pydantic 模型派生", bool(schema_fmt and "topic" in str(schema_fmt.get("json_schema"))))
    print(f"   带 json_model → {str(schema_fmt)[:100]}...")

    def call(label: str, **kw):
        try:
            c = gw.chat(user_id="smoke_llm", **kw)
            print(
                f"       → in={c.prompt_tokens} out={c.completion_tokens} "
                f"cached={c.cached_tokens} finish={c.finish_reason}"
            )
            return c
        except Exception as e:  # noqa: BLE001 — 冒烟脚本要报出任何失败原因
            check(label, False, f"{type(e).__name__}: {e}")
            return None

    # ---- 1) 基础对话 -------------------------------------------------------
    print("\n→ 1/5 基础对话（multi_turn_dialogue，自由文本）...")
    c = call(
        "基础对话",
        task_type="multi_turn_dialogue",
        messages=[
            {"role": "system", "content": "你是简洁助手，只回答一个字。"},
            {"role": "user", "content": "请只回复：通"},
        ],
    )
    check("基础对话有返回", c is not None and bool(c.text.strip()), f"text={c.text!r}" if c else "")

    # ---- 2) 思考模式（直调 Provider；网关的 reasoning 由策略表决定）----------
    print("\n→ 2/5 思考模式（直调 Provider，reasoning=on）...")
    try:
        c = real.chat(
            model=model, reasoning=True,
            messages=[{"role": "user", "content": "1+1=? 只回数字"}],
            task_type="deep_reasoning",
        )
        print(f"       → in={c.prompt_tokens} out={c.completion_tokens} finish={c.finish_reason}")
        check("思考模式有返回", bool(c.text.strip()), f"text={c.text!r}")
        # 思考 token 计入 output：同等问题下 on 通常明显多于 off，作为「开关真的生效」的旁证
        check("思考 token 计入 output（out 明显大于裸答案）", c.completion_tokens >= 5,
              f"out={c.completion_tokens}")
    except Exception as e:  # noqa: BLE001
        check("思考模式有返回", False, f"{type(e).__name__}: {e}")

    # ---- 3) function calling ----------------------------------------------
    print("\n→ 3/5 function calling...")
    tools = [
        {
            "type": "function",
            "function": {
                "name": "save_note",
                "description": "保存一条笔记",
                "parameters": {
                    "type": "object",
                    "properties": {"title": {"type": "string"}, "content": {"type": "string"}},
                    "required": ["title", "content"],
                },
            },
        }
    ]
    c = call(
        "function calling",
        task_type="multi_turn_dialogue",  # 工具调用不应被 JSON 约束干扰
        messages=[{"role": "user", "content": "帮我记一条笔记：标题「连通性检查」，内容是「ok」"}],
        tools=tools,
    )
    if c and "tool_request" in (c.text or ""):
        check("工具调用被解析成 [tool_request]", True, c.text[:120])
    else:
        # 不判失败：是否调工具取决于模型风格；这里只要求链路不报错
        print(f"   [INFO] 本次未触发工具调用（模型风格差异，不判失败）text={((c.text if c else '') or '')[:80]!r}")

    # ---- 4) JSON 任务经网关 -----------------------------------------------
    print("\n→ 4/5 JSON 任务经网关（topic_analysis，自动带 json_object）...")
    c = call(
        "JSON 任务",
        task_type="topic_analysis",
        messages=[
            {"role": "system", "content": '只输出 JSON：{"topics":[{"topic":"测试","count":1}]}'},
            {"role": "user", "content": "给一条包含 topics 的 JSON"},
        ],
    )
    parsed = None
    if c:
        raw = c.text.strip()
        for t in (raw, raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()):
            try:
                parsed = json.loads(t, strict=False)
                break
            except json.JSONDecodeError:
                continue
    check("JSON 任务回包可解析", parsed is not None, f"parsed={parsed}" if parsed else f"text={(c.text if c else '')[:160]!r}")

    print("\n" + ("模型层连通性全部通过 ✅" if ok else "存在失败项 ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
