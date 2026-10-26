"""模型可触达面的白名单（harness 层，不含任何业务逻辑）。

这个文件只回答一个问题：**模型这一侧现在能碰到什么**。四类边界在这里显式登记，
`scripts/harness_check.py` 逐条与代码对账，`tests/test_harness_surface.py` 盯着对账结果。

三条原则：

1. **默认关。** 清单里没有的，一律不给。新增要先写清单，再写代码。
2. **登记 ≠ 放开。** 登记的作用是让新增**显式**，不是让新增**容易**。
3. **运行时只强拦成本低的那一项。** 也就 `ALLOWED_TOOLS`——它是每轮调用的必经点，
   拦错立刻报、不会误伤用户在途请求。任务类型与出网主机只做静态对账：
   这两类在运行中抛错会把一次正常的用户请求打成 500，**红测试比炸线上划算**。

本轮的事实基线（2026-10-26 扫描，见 `docs/harness/模型可触达面清单.md`）：

- **工具**：一个，且只在诊断链路。全仓传 `tools` 的调用点只有
  `scripts/smoke_llm.py` 的「function calling 连通性检查」；`app/llm/gateway.py`
  只做透传。这个项目的模型只做「文本进 → 文本 / JSON 出」，
  **业务链没有任何调用方传过工具**。白名单写的是这个事实，不是让人安心的空集。
- **文件**：模型层零文件 IO。喂给模型的只有拼装好的 prompt 字符串，
  书籍 / PDF / epub 的解析全部发生在 `app/books/`（业务层），模型看不到路径。
- **密钥**：模型层只该见到供应商 Key。`jwt_secret` 与 `weread_api_key` 登记为
  「模型层不可见」，由 `assert_secret_scope` 做静态声明、由清单文档对外可读。
"""
from __future__ import annotations

# ---- 工具（模型可主动调用的函数）--------------------------------------------
# 工具名在这里指 function calling 的 `name`，**不是**业务能力
# （能力是本地代码分派，模型只输出路由 JSON，从不调工具）。
#
# 扫描结果（2026-10-26）：全仓唯一传 tools 的地方是 scripts/smoke_llm.py 的
# 「function calling 连通性检查」——它必须真发一次工具定义，才能证明供应商链路
# 没坏。业务链**零调用方**。所以白名单里只有这一个诊断用工具，而不是空集：
# 清单要写事实，不写让人安心的假象。
TOOL_PURPOSE: dict[str, str] = {
    "save_note": "仅 scripts/smoke_llm.py 的链路诊断使用；业务链零调用方，模型在真实对话里不会被给到任何工具",
}
ALLOWED_TOOLS: frozenset[str] = frozenset(TOOL_PURPOSE)

# 允许传 tools 的文件。除此之外任何地方传工具，都是绕过白名单。
TOOL_CALLSITE_ALLOWLIST: frozenset[str] = frozenset({
    "app/llm/gateway.py",      # 唯一透传点
    "scripts/smoke_llm.py",    # 诊断脚本（不是业务链）
})

# ---- 任务类型（模型被要求做的事）--------------------------------------------
# 与 `gateway.STRATEGY_TABLE` 的键**必须完全相等**：少了是漏登记，多了是幽灵项。
# 相等性由 harness_check 断言——新增一行策略表而忘了登记，检查立刻红。
ALLOWED_TASK_TYPES: frozenset[str] = frozenset({
    "default",
    "multi_turn_dialogue",
    "deep_reasoning",
    "conflict_detection",
    "conflict_review",
    "plan_generation",
    "causal_reasoning",
    "cognitive_brief",
    "l1_mining",
    "batch_extraction",
    "topic_analysis",
    "capability_routing",
    "book_digest",
    "book_recommend",
    "reading_suggestion",
    "conflict_attribution",
    "probe_question",
    "book_compare",
})

# ---- 网络（出网主机）--------------------------------------------------------
# 扫描范围：app/ 与 scripts/ 里的全部 http(s) 字面量（含注释里的文档链接——
# 宁可多登记一个不可达的主机，也不要让注释里的斜体URL变成漏网的理由）。
EGRESS_PURPOSE: dict[str, str] = {
    "api.deepseek.com": "模型供应商（唯一在用）",
    "api.xiaomimimo.com": "模型供应商（已停用，Key 留空即摘链）",
    "i.weread.qq.com": "微信读书同步网关（非模型层）",
    "weread.qq.com": "微信读书授权页（注释中的文档链接，运行时不请求）",
    "cdn.weread.qq.com": "官方技能包下载地址（注释，运行时不请求）",
    "platform.xiaomimimo.com": "供应商控制台（注释，运行时不请求）",
    "hf-mirror.com": "HuggingFace 镜像（仅 embedding_backend=bge 时下载）",
    "www.modelscope.cn": "embedding 模型预取（仅 scripts/fetch_embedding_model.py）",
}

ALLOWED_EGRESS_HOSTS: frozenset[str] = frozenset(EGRESS_PURPOSE)

# ---- 密钥（进程能读到的凭据）------------------------------------------------
# model_visible=False 的条目在语义上**不得**流进 prompt；这里不做运行时脱敏
# （那属于响应体统一，已列入暂缓项），只做登记与清单可见。
SECRET_FIELDS: dict[str, dict[str, object]] = {
    "deepseek_api_key": {"purpose": "模型供应商鉴权", "model_visible": True},
    "mimo_api_key": {"purpose": "模型供应商鉴权（已停用）", "model_visible": True},
    "weread_api_key": {"purpose": "微信读书账号凭据（只读个人数据）", "model_visible": False},
    "jwt_secret": {"purpose": "会话签名 / 验签", "model_visible": False},
}

# ---- 文件（模型层可见的本地路径）--------------------------------------------
# 空集：模型看不到任何路径，只有拼好的 prompt 文本。模型层（app/llm/）出现文件 IO
# 即视为越界——那是把本地内容往 prompt 里塞的高危入口。
MODEL_LAYER_DIR = "app/llm"
FILE_IO_PATTERNS: tuple[str, ...] = ("open(", "read_bytes(", "read_text(", "Path(")


class PolicyViolation(RuntimeError):
    """边界被越过。**故意不继承 LLMError**：LLMError 会触发供应商回退与重试，
    把「有人在偷偷加工具」变成「换个供应商再试一次」，越权就被悄悄吃掉了。"""


def assert_tools_allowed(tools: list | None) -> None:
    """调用前拦一道：传进来的工具名必须全在白名单里。

    `tools=None` / 空列表直接放行（当前全部调用都是这种）。形状不认识时也放行——
    网关不该替调用方解析工具定义，真正的护栏是「白名单为空」这件事本身。
    """
    if not tools:
        return
    names: set[str] = set()
    for item in tools:
        if isinstance(item, dict):
            fn = item.get("function") if isinstance(item.get("function"), dict) else item
            name = fn.get("name") if isinstance(fn, dict) else None
            if isinstance(name, str) and name:
                names.add(name)
    illegal = sorted(names - ALLOWED_TOOLS)
    if illegal:
        raise PolicyViolation(
            f"工具未登记于白名单：{illegal}。当前允许的工具是 {sorted(ALLOWED_TOOLS) or '空集'}；"
            "要放开请先改 app/llm/policy.py 的 ALLOWED_TOOLS 并补测试。"
        )
