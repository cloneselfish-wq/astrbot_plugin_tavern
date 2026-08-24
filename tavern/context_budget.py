"""Provider-aware hard input budget guard used before every model call."""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_CONFIGURED_INPUT_BUDGET = 12_000
DEFAULT_PROVIDER_CONTEXT_WINDOW = 16_384
PROTOCOL_RESERVE_TOKENS = 1_024


@dataclass(frozen=True, slots=True)
class InputBudgetReceipt:
    configured_input_budget: int
    provider_context_window: int
    reserved_output_tokens: int
    protocol_reserve: int
    effective_input_budget: int
    estimated_input_tokens: int
    within_budget: bool


def estimate_tokens(value: str) -> int:
    # Deliberately conservative for mixed Chinese/JSON when no provider tokenizer exists.
    return max(1, (len(str(value or "")) + 2) // 3)


def input_budget_receipt(
    prompt: str,
    system_prompt: str,
    *,
    reserved_output_tokens: int,
    configured_input_budget: int = DEFAULT_CONFIGURED_INPUT_BUDGET,
    provider_context_window: int = DEFAULT_PROVIDER_CONTEXT_WINDOW,
) -> InputBudgetReceipt:
    output = max(1, int(reserved_output_tokens))
    provider_window = max(output + PROTOCOL_RESERVE_TOKENS + 1, int(provider_context_window))
    effective = min(
        max(1, int(configured_input_budget)),
        provider_window - output - PROTOCOL_RESERVE_TOKENS,
    )
    estimated = estimate_tokens(str(system_prompt or "") + "\n" + str(prompt or ""))
    return InputBudgetReceipt(
        configured_input_budget=max(1, int(configured_input_budget)),
        provider_context_window=provider_window,
        reserved_output_tokens=output,
        protocol_reserve=PROTOCOL_RESERVE_TOKENS,
        effective_input_budget=effective,
        estimated_input_tokens=estimated,
        within_budget=estimated <= effective,
    )


def enforce_hard_input_budget(
    prompt: str,
    system_prompt: str,
    *,
    reserved_output_tokens: int,
    configured_input_budget: int = DEFAULT_CONFIGURED_INPUT_BUDGET,
    provider_context_window: int = DEFAULT_PROVIDER_CONTEXT_WINDOW,
) -> InputBudgetReceipt:
    receipt = input_budget_receipt(
        prompt,
        system_prompt,
        reserved_output_tokens=reserved_output_tokens,
        configured_input_budget=configured_input_budget,
        provider_context_window=provider_context_window,
    )
    if not receipt.within_budget:
        raise ValueError(
            "生成上下文超过当前模型的安全输入预算；系统未发起模型请求，"
            "已保留副本状态。请缩短最近历史或切换支持更大上下文的模型后重试。"
        )
    return receipt


__all__ = [
    "DEFAULT_CONFIGURED_INPUT_BUDGET",
    "DEFAULT_PROVIDER_CONTEXT_WINDOW",
    "InputBudgetReceipt",
    "PROTOCOL_RESERVE_TOKENS",
    "enforce_hard_input_budget",
    "estimate_tokens",
    "input_budget_receipt",
]
