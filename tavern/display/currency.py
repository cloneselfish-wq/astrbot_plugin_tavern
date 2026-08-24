from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any


DEFAULT_CURRENCY_LABELS = {
    "copper": ("铜币", "铜币", "🪙"),
    "silver": ("银币", "银币", "◉"),
    "gold": ("金币", "金币", "✦"),
}


@dataclass(frozen=True, slots=True)
class CurrencyView:
    currency_id: str
    label: str
    short_label: str
    icon: str
    precision: int
    amount_minor: int
    amount: int | float
    formatted_amount: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def currency_view(
    currency_id: str,
    amount_minor: Any,
    *,
    precision: int = 0,
    label: str = "",
    short_label: str = "",
    icon: str = "",
) -> CurrencyView:
    stable_id = str(currency_id or "").strip()
    defaults = DEFAULT_CURRENCY_LABELS.get(
        stable_id,
        (stable_id or "货币", stable_id or "货币", "🪙"),
    )
    display_label = str(label or defaults[0])
    display_short = str(short_label or defaults[1])
    display_icon = str(icon or defaults[2])
    digits = max(0, int(precision or 0))
    minor = int(amount_minor or 0)
    amount_decimal = Decimal(minor) / (Decimal(10) ** digits)
    amount: int | float = (
        int(amount_decimal)
        if amount_decimal == amount_decimal.to_integral()
        else float(amount_decimal)
    )
    formatted = (
        f"{amount} 枚{display_short}"
        if digits == 0
        else f"{amount} {display_short}"
    )
    return CurrencyView(
        currency_id=stable_id,
        label=display_label,
        short_label=display_short,
        icon=display_icon,
        precision=digits,
        amount_minor=minor,
        amount=amount,
        formatted_amount=formatted,
    )


def format_money(
    currency_id: str,
    amount_minor: Any,
    *,
    precision: int = 0,
    label: str = "",
    short_label: str = "",
    icon: str = "",
) -> str:
    return currency_view(
        currency_id,
        amount_minor,
        precision=precision,
        label=label,
        short_label=short_label,
        icon=icon,
    ).formatted_amount
