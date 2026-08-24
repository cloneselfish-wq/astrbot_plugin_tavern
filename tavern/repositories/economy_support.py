"""A16：可选多货币经济系统领域仓库（世界包 economy 块驱动，默认关闭）。

设计要点：
- 完全可选：未启用或世界包未声明 `rules.economy` 时，任何调用都返回「未启用」，
  不自动创建货币/钱包，不干扰旧副本。
- 稳定 ID：钱包绑定 owner_type + owner_ref（稳定 ID），不绑定显示名称。
- 金额以「最小单位整数」存储（精度由货币定义决定），避免浮点漂移。
- 幂等：`operation_id` 全局唯一；重复提交返回首次结果，杜绝重复扣款/重复到账。
- 并发：所有写操作在 `BEGIN IMMEDIATE` 单事务内完成，余额读改写互斥。
- 标准化结果：成功/失败都返回 {ok, operation_id, currency_id, amount,
  balance_before, balance_after, reason, actor_id, created_at, ...}。
"""
from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from ..database_support import clean_text, new_id, json_dump, json_load, utc_now
from ..errors import EconomyDisabledError, InsufficientFundsError
from ..display.currency import currency_view, format_money

logger = logging.getLogger(__name__)

ECONOMY_SOURCE_STORY = "story"
ECONOMY_SOURCE_ADMIN = "admin"
ECONOMY_SOURCE_DM = "dm"
ECONOMY_SOURCE_WEB = "web"
ECONOMY_SOURCE_WORLD = "world"


def _major_to_minor(value: Any, precision: int) -> int:
    """把主单位金额（str/int/float/Decimal）转为最小单位整数。"""
    try:
        dec = Decimal(str(value or "0")).quantize(
            Decimal(1).scaleb(-int(precision))
        )
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"金额格式无效：{value}") from None
    return int(dec.scaleb(int(precision)))


def _minor_to_major(value: int, precision: int) -> str:
    if not int(precision):
        return str(int(value))
    return format(Decimal(int(value)).scaleb(-int(precision)), "f")



__all__ = [name for name in globals() if not name.startswith('__')]
