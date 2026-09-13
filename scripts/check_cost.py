"""校准计费：发两次完全相同的请求，看真实 token / 缓存命中与估算费用。

用法（项目根目录，KEY 从 .env 读取，无需在命令行传）：
    python scripts/check_cost.py

为什么要发两次：DeepSeek 的自动上下文缓存按**前缀**复用，第一次必然未命中，
第二次通常会命中——因此第二次的 cached_tokens 非零即说明缓存记账链路通了。
若第二次仍为 0，说明字段名与线上不符（见 DeepSeekProvider._cached_tokens），
此时成本会被高估（缓存命中价约为未命中的 1/50），但不影响功能。
"""
from __future__ import annotations

import sys

from app.core.config import settings
from app.llm.cost import _estimate_cost, is_peak_time
from app.llm.gateway import ModelGateway


def main() -> int:
    if settings.model_provider != "deepseek":
        print("[FAIL] 未配置 DEEPSEEK_API_KEY，当前为 MockProvider，无法校准计费")
        return 1

    peak = is_peak_time()
    print(f"模型：{settings.deepseek_model}")
    print(f"时段：{'高峰' if peak else '空闲'}（倍率 {settings.deepseek_peak_multiplier if peak else 1.0}）")
    print(
        f"单价（元/百万 token）：未命中 {settings.deepseek_price_input_per_1m} / "
        f"命中 {settings.deepseek_price_cache_hit_per_1m} / 输出 {settings.deepseek_price_output_per_1m}"
    )

    gateway = ModelGateway()
    # 前缀稳定：同一段 system + 同一段长文本，第二次应命中缓存
    long_prefix = "认知副驾计费校准用固定前缀。" * 60
    messages = [
        {"role": "system", "content": "你是计费校准器，只回复 OK。"},
        {"role": "user", "content": f"{long_prefix}\n请只回复 OK。"},
    ]

    first = gateway.chat(task_type="batch_extraction", messages=messages, user_id="cost_check")
    second = gateway.chat(task_type="batch_extraction", messages=messages, user_id="cost_check")

    print()
    ok = True
    for label, completion in (("第一次", first), ("第二次", second)):
        cost = _estimate_cost(
            completion.prompt_tokens, completion.completion_tokens, completion.cached_tokens
        )
        print(
            f"{label}：输入 {completion.prompt_tokens} token"
            f"（其中缓存命中 {completion.cached_tokens}）"
            f"，输出 {completion.completion_tokens} token，估算 {cost:.6f} 元"
        )

    if first.prompt_tokens <= 0 or second.prompt_tokens <= 0:
        print("\n[FAIL] 未取到 token 计数，计费链路有问题")
        ok = False
    if second.cached_tokens == 0:
        print(
            "\n[WARN] 第二次仍未命中缓存。可能原因：前缀未达最小可缓存长度、缓存尚未建立，"
            "或线上字段名与 _cached_tokens 的读取路径不一致。"
            "\n        影响：成本被高估（命中价约为未命中的 1/50），功能不受影响。"
        )
        ok = False
    elif second.cached_tokens >= second.prompt_tokens * 0.5:
        saved = _estimate_cost(second.prompt_tokens, second.completion_tokens, 0) - _estimate_cost(
            second.prompt_tokens, second.completion_tokens, second.cached_tokens
        )
        print(f"\n缓存命中占输入 {second.cached_tokens / second.prompt_tokens:.0%}，本次省下约 {saved:.6f} 元")

    print("\n" + ("计费链路正常 ✅" if ok else "计费链路需人工确认 ⚠️"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())