"""统一模型网关（§3.1 唯一收口点）。

职责：策略路由（task_type → reasoning 开关）→ Provider 调用 → 重试 → 供应商回退
→ 结构化输出校验与修复 → 成本落库。业务层不感知供应商细节；全链路 request_id 在这里贯穿。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core import trace
from app.core.config import settings
from app.llm import cost as cost_service
from app.llm.completion import Completion
from app.llm.exceptions import LLMError, RetryableLLMError
from app.llm.prompts import PROMPT_VERSION_BY_TASK_TYPE
from app.llm.provider import LLMProvider
from app.llm.retry import with_retry
from app.llm.structure import build_repair_messages, parse_structured

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Strategy:
    task_type: str
    reasoning: bool
    model: str  # 空 → 运行时回落 settings.active_model（按当前供应商取）
    prompt_version: str = ""  # 提示词版本（E 组），成本落库追溯用


# 静态映射（配表驱动，可热更新；缺省为"默认对话 reasoning=off"）
# reasoning 开关与设计文档 §3.1.2 对齐：深度/冲突/归因任务开思考，标准对话/规划/简报关思考省成本。
# （曾因骨架期笔误把 multi_turn_dialogue/plan_generation/cognitive_brief 写成 on，
#   已校回 off——这三类均非深度推理任务，且 reasoning=on 的单价显著更高。）
STRATEGY_TABLE: dict[str, Strategy] = {
    "default": Strategy("default", False, ""),
    "multi_turn_dialogue": Strategy("multi_turn_dialogue", False, ""),
    "deep_reasoning": Strategy("deep_reasoning", True, ""),
    "conflict_detection": Strategy("conflict_detection", True, ""),
    # 复核裁判：比初判更需要推理（先自辩再终判），reasoning=on；
    # 走独立 task_type 便于在 cost_logs 里单独审计复核花费
    "conflict_review": Strategy("conflict_review", True, ""),
    "plan_generation": Strategy("plan_generation", False, ""),
    "causal_reasoning": Strategy("causal_reasoning", True, ""),
    "cognitive_brief": Strategy("cognitive_brief", False, ""),
    "l1_mining": Strategy("l1_mining", True, ""),  # L1 模糊意图→定位判断（reasoning=on）
    "batch_extraction": Strategy("batch_extraction", False, ""),
    "topic_analysis": Strategy("topic_analysis", False, ""),
    # 对话入口的意图分流：**每轮都会跑**，且本地命令式规则已兜住高价值指令，
    # 判不出来时回落通用对话也是安全行为——故 reasoning=off 控成本。
    # 若实测分流质量不足，再单独把这行改成 on（比照 l1_mining）。
    "capability_routing": Strategy("capability_routing", False, ""),
    # 书籍三项：通读的分块提要点与汇总、书籍推荐都是标准结构化任务，reasoning=off；
    # 讨论复用 multi_turn_dialogue 策略（自然语言、off），不单开一行
    "book_digest": Strategy("book_digest", False, ""),
    "book_recommend": Strategy("book_recommend", False, ""),
}


def strategy_for(task_type: str = "default") -> Strategy:
    return STRATEGY_TABLE.get(task_type, STRATEGY_TABLE["default"])


# 期望严格 JSON 输出的任务类型（对应提示词里那句「输出严格 JSON」）；
# 唯一例外是 multi_turn_dialogue（自然语言回复）与 default（兜底，用途不定）。
# 供应商声明 supports_json_object / supports_json_schema 时，这些任务会自动带上
# response_format——把「输出必须合法 / 必须是这个形状」讲给服务端听，而不是等
# 模型自由发挥后再本地校验失败（L3 简报实测踩到过，详见 dev_logs/复盘与教训.md #16）。
JSON_TASK_TYPES: frozenset[str] = frozenset({
    "capability_routing",
    "l1_mining",
    "batch_extraction",
    "conflict_detection",
    "conflict_review",
    "topic_analysis",
    "cognitive_brief",
    "plan_generation",
    "causal_reasoning",
    "deep_reasoning",
    "book_digest",
    "book_recommend",
})


def _build_provider_chain() -> list[LLMProvider]:
    """按优先级构建供应商链：mimo > deepseek > mock。

    两个 KEY 都配了 → 链上有两个，主供应商失败时自动切备用（LLM-4 运行时回退）。
    完全没有 KEY → 退化为 MockProvider（确定性、零成本），**不会**在真实调用失败时
    悄悄用 Mock 兜住——那会把「模型挂了」伪装成「模型答了」。
    LLM_FALLBACK_ENABLED=false → 只保留主供应商。
    """
    chain: list[LLMProvider] = []
    if settings.mimo_api_key:
        from app.llm.mimo.provider import MiMoProvider
        chain.append(MiMoProvider())
    if settings.deepseek_api_key:
        from app.llm.deepseek.provider import DeepSeekProvider
        chain.append(DeepSeekProvider())
    if not chain:
        from app.llm.deepseek.mock import MockProvider
        logger.info("未配置 MIMO_API_KEY / DEEPSEEK_API_KEY，网关路由到 MockProvider")
        chain.append(MockProvider())
    elif len(chain) > 1 and not settings.llm_fallback_enabled:
        logger.info("供应商回退已关闭（LLM_FALLBACK_ENABLED=false），只用 %s", type(chain[0]).__name__)
        chain = chain[:1]
    return chain


def _build_provider() -> LLMProvider:
    """当前主供应商（脚本/测试用于窥探模型层配置）。"""
    return _build_provider_chain()[0]


def _schema_error(text: str, json_model: type) -> str | None:
    """按 pydantic 模型校验模型输出；返回失败原因，None 表示通过。"""
    try:
        parse_structured(text, validator=lambda d: json_model.model_validate(d))
    except LLMError as exc:  # JsonParseError 亦属 LLMError
        return str(exc)
    return None


class ModelGateway:
    """所有模型调用的唯一入口（可替换性抽象落地的门面）。"""

    def __init__(
        self,
        provider: LLMProvider | None = None,
        providers: list[LLMProvider] | None = None,
    ):
        """provider = 单个供应商（测试注入，不参与回退）；providers = 整条链（显式指定优先级）。"""
        if providers is not None:
            self._providers = list(providers)
        elif provider is not None:
            self._providers = [provider]
        else:
            self._providers = _build_provider_chain()
        if not self._providers:
            raise ValueError("供应商链不能为空")
        self._provider = self._providers[0]  # 主供应商（能力位判定 / 向后兼容取用）

    @property
    def has_real_provider(self) -> bool:
        """链上是否存在真实供应商（Mock-only = 未配置任何 Key）。"""
        return any(not getattr(p, "is_mock", False) for p in self._providers)

    @property
    def real_provider_names(self) -> list[str]:
        """真实供应商类名列表（doctor / 诊断信息展示用）。"""
        return [type(p).__name__ for p in self._providers if not getattr(p, "is_mock", False)]

    def route(self, task_type: str = "default") -> Strategy:
        s = strategy_for(task_type)
        return Strategy(
            s.task_type,
            s.reasoning,
            s.model or settings.active_model,
            prompt_version=PROMPT_VERSION_BY_TASK_TYPE.get(s.task_type, ""),
        )

    def chat(
        self,
        *,
        task_type: str = "default",
        messages: list[dict],
        user_id: str = "anonymous",
        session: Session | None = None,
        tools: list | None = None,
        response_format: dict | None = None,
        prompt_version: str = "",
        json_model: type | None = None,
    ) -> Completion:
        """发起一次模型调用：路由 → 重试 → 回退 → 结构化校验（必要时修复）→ 成本埋点。

        prompt_version 显式传入时覆盖策略表默认值（一个 task_type 对应多个提示词的
        场景——如 cognitive_brief 同时服务简报与衔接追问——由调用方区分版本）。

        json_model 传入该任务用于结构校验的 pydantic 模型时，网关会做两件事：
        1. 从它派生 json_schema 交给支持原生 JSON 约束的供应商（服务端约束）；
        2. 回包仍不合结构时，发起**一次**修复回合（把破损输出与失败原因回灌给模型）。
        调用方照旧只负责 `parse_structured` 解析与降级，不需要感知这一层。
        """
        strat = self.route(task_type)
        completion = self._call_with_retry(strat, messages, tools, response_format, json_model)
        self._record_cost(session, user_id, strat, completion, prompt_version)

        if json_model is not None and settings.llm_json_repair_enabled:
            completion = self._repair_if_invalid(
                strat, completion, messages, tools, response_format, json_model,
                session=session, user_id=user_id, prompt_version=prompt_version,
            )
        return completion

    # ---- 内部：约束 / 调用 / 回退 / 修复 / 记账 ---------------------------------

    def _effective_response_format(
        self,
        task_type: str,
        explicit: dict | None,
        json_model: type | None = None,
        provider: LLMProvider | None = None,
    ) -> dict | None:
        """决定本次调用带什么 response_format。

        优先级：调用方显式指定 > 从 json_model 派生 json_schema > JSON 任务兜底 json_object
        > 不带（自由文本）。供应商不支持时一律不带，行为与从前一致。

        provider 用于**回退场景**：同一份请求切到备用供应商时，约束要按备用供应商的能力重算，
        否则会把备用供应商不认识的 response_format 发过去（请求直接被拒）。
        """
        target = provider or self._provider
        if explicit is not None:
            return explicit
        if json_model is not None and getattr(target, "supports_json_schema", False):
            return {
                "type": "json_schema",
                "json_schema": {
                    "name": json_model.__name__,
                    "schema": json_model.model_json_schema(),
                    "strict": True,
                },
            }
        if task_type in JSON_TASK_TYPES and getattr(target, "supports_json_object", False):
            return {"type": "json_object"}
        return None

    def _call_with_retry(
        self,
        strat: Strategy,
        messages: list[dict],
        tools: list | None,
        response_format: dict | None,
        json_model: type | None = None,
    ) -> Completion:
        """按供应商链依次尝试：主供应商重试 3 次仍失败 → 自动切下一个供应商。

        回退的判定口径是 LLMError——既包含「重试耗尽的可重试错误」（限流 / 超时 / 5xx），
        也包含「不可重试错误」（401 鉴权失败、400 模型名错、配额耗尽）。这类失败换一个
        供应商往往就通了，而用户在界面上看到的是能力正常出结果，不是一句报错。
        """
        last_exc: LLMError | None = None
        for index, provider in enumerate(self._providers):
            if index:
                logger.warning(
                    "主供应商失败，回退到 %s task=%s err=%s req=%s",
                    type(provider).__name__, strat.task_type, last_exc, trace.get_request_id(),
                )
            # 每个供应商用自己的默认模型名：主供应商的模型名发给备用供应商会被判
            # Unsupported model，等于回退必然失败。
            model = getattr(provider, "default_model", "") or strat.model
            fmt = self._effective_response_format(strat.task_type, response_format, json_model, provider)
            try:
                return self._invoke_with_retry(strat, provider, model, messages, tools, fmt)
            except LLMError as exc:
                last_exc = exc
                if index + 1 == len(self._providers):
                    raise
        assert last_exc is not None  # 循环必在最后一个供应商处返回或抛出
        raise last_exc

    def _invoke_with_retry(
        self,
        strat: Strategy,
        provider: LLMProvider,
        model: str,
        messages: list[dict],
        tools: list | None,
        response_format: dict | None,
    ) -> Completion:
        def invoke() -> Completion:
            return provider.chat(
                model=model,
                reasoning=strat.reasoning,
                messages=messages,
                tools=tools,
                response_format=response_format,
                task_type=strat.task_type,
            )

        def on_retry(attempt: int, exc: BaseException) -> None:
            logger.warning(
                "llm retry attempt=%d provider=%s task=%s err=%s req=%s",
                attempt, type(provider).__name__, strat.task_type, exc, trace.get_request_id(),
            )

        start = time.monotonic()
        result = with_retry(invoke, attempts=3, retry_exceptions=(RetryableLLMError,), on_retry=on_retry)
        logger.info(
            "llm ok task=%s provider=%s model=%s reasoning=%s in=%d out=%d dur=%.0fms req=%s",
            strat.task_type, type(provider).__name__, model, strat.reasoning,
            result.prompt_tokens, result.completion_tokens,
            (time.monotonic() - start) * 1000, trace.get_request_id(),
        )
        return result

    def _repair_if_invalid(
        self,
        strat: Strategy,
        completion: Completion,
        messages: list[dict],
        tools: list | None,
        response_format: dict | None,
        json_model: type,
        *,
        session: Session | None,
        user_id: str,
        prompt_version: str,
    ) -> Completion:
        """结构化输出校验失败时补一次修复回合；仍失败则**原样返回**原始输出。

        原样返回是刻意的：调用方已有成熟降级路径（L2 丢弃该对、L3 出空简报、分流回落
        通用对话），网关不该在这里改语义，只负责多给模型一次机会。
        """
        error = _schema_error(completion.text, json_model)
        if error is None:
            return completion

        logger.warning(
            "结构化输出未通过校验，发起一次修复回合 task=%s err=%s req=%s",
            strat.task_type, error, trace.get_request_id(),
        )
        try:
            fixed = self._call_with_retry(
                strat, build_repair_messages(
                    messages, completion.text, error,
                    schema_fields=list(json_model.model_fields),
                ),
                tools, response_format, json_model,
            )
        except LLMError as exc:
            logger.warning("修复回合调用失败，沿用原始输出 task=%s err=%s", strat.task_type, exc)
            return completion

        # 修复回合是真实调用，token 同样要记账（否则这部分开销完全不可见）
        self._record_cost(session, user_id, strat, fixed, prompt_version)
        if _schema_error(fixed.text, json_model) is None:
            logger.info("修复回合成功 task=%s req=%s", strat.task_type, trace.get_request_id())
            return fixed
        logger.warning("修复回合仍未通过校验，沿用原始输出 task=%s", strat.task_type)
        return completion

    def _record_cost(
        self,
        session: Session | None,
        user_id: str,
        strat: Strategy,
        completion: Completion,
        prompt_version: str,
    ) -> None:
        cost_service.record_cost(
            session,
            user_id=user_id,
            task_type=strat.task_type,
            # 记**实际服务**的模型名：发生回退时它不再是主供应商的模型，费用要按它算
            model=completion.model or strat.model,
            reasoning=strat.reasoning,
            completion=completion,
            prompt_version=prompt_version or strat.prompt_version,
        )
