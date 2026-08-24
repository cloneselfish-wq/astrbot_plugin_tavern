"""D1-RUN-009 / D1-ARC-002 的「请求分类 -> 模型路由」纯逻辑层。

本模块不执行任何 I/O：
- ``classify_request`` 把外部请求归类到稳定任务类别；
- ``ModelRouter.route`` 按类别选择模型路由（模型名由宿主装配时映射到实际供应商）。

对应 D1-ARC-002 3.2 权威执行顺序的第一步与第二步。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


PROPOSAL_SCHEMA = "tavern-ai-proposal/1.0.0-rc10"

# 稳定任务类别：新增类别必须同时补充默认路由，避免模型路由静默回退。
TASK_CATEGORIES = frozenset(
    {
        "player_choice",
        "narrative",
        "dm_control",
        "fate_consequence",
        "terminal_check",
        "routine",
    }
)

# 风险等级：低/中/高/终局。终局路由只允许结构化核算，禁止生成叙事自由发挥。
RISK_LEVELS = frozenset({"low", "medium", "high", "terminal"})


@dataclass(frozen=True)
class ModelRoute:
    """一次模型调用的确定性路由描述（纯数据，无 I/O）。"""

    category: str
    model: str
    prompt_ref: str
    structured_schema: str
    risk_level: str
    max_tokens: int
    temperature: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "model": self.model,
            "prompt_ref": self.prompt_ref,
            "structured_schema": self.structured_schema,
            "risk_level": self.risk_level,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }


DEFAULT_ROUTES: dict[str, ModelRoute] = {
    "player_choice": ModelRoute(
        category="player_choice",
        model="choice",
        prompt_ref="prompt:player_choice",
        structured_schema=PROPOSAL_SCHEMA,
        risk_level="medium",
        max_tokens=1200,
        temperature=0.7,
    ),
    "narrative": ModelRoute(
        category="narrative",
        model="narrative",
        prompt_ref="prompt:narrative",
        structured_schema=PROPOSAL_SCHEMA,
        risk_level="medium",
        max_tokens=2400,
        temperature=0.8,
    ),
    "dm_control": ModelRoute(
        category="dm_control",
        model="dm",
        prompt_ref="prompt:dm_control",
        structured_schema=PROPOSAL_SCHEMA,
        risk_level="high",
        max_tokens=2400,
        temperature=0.6,
    ),
    "fate_consequence": ModelRoute(
        category="fate_consequence",
        model="fate",
        prompt_ref="prompt:fate_consequence",
        structured_schema=PROPOSAL_SCHEMA,
        risk_level="high",
        max_tokens=1200,
        temperature=0.4,
    ),
    "terminal_check": ModelRoute(
        category="terminal_check",
        model="terminal",
        prompt_ref="prompt:terminal_check",
        structured_schema=PROPOSAL_SCHEMA,
        risk_level="terminal",
        max_tokens=800,
        temperature=0.0,
    ),
    "routine": ModelRoute(
        category="routine",
        model="routine",
        prompt_ref="prompt:routine",
        structured_schema=PROPOSAL_SCHEMA,
        risk_level="low",
        max_tokens=600,
        temperature=0.0,
    ),
}


def classify_request(request: Mapping[str, Any]) -> str:
    """把请求映射到稳定任务类别（确定性规则，不允许自由裁量）。

    判定优先级（先命中先返回）：
    1. 显式终局核算（``kind == "terminal_check"`` 或 ``purpose == "terminal"``）；
    2. 系统例行内务（重试/提醒/维护）；
    3. 主持叙事控制（``source == "dm"``）；
    4. 玩家行动选择；
    5. 命运后果结构化提案；
    6. 其余一律归为叙事推进。
    """

    request = dict(request or {})
    source = str(request.get("source") or "").strip().lower()
    kind = str(request.get("kind") or request.get("action") or "").strip().lower()
    purpose = str(request.get("purpose") or "").strip().lower()
    if kind == "terminal_check" or purpose == "terminal":
        return "terminal_check"
    if source == "system" and kind in {"retry", "reminder", "maintenance"}:
        return "routine"
    if source == "dm":
        return "dm_control"
    if source == "player" and kind in {"choice", "action", "vote"}:
        return "player_choice"
    if kind in {"fate", "consequence"} or purpose == "fate":
        return "fate_consequence"
    return "narrative"


class ModelRouter:
    """按任务类别选择模型路由；类别未注册时显式失败，不做隐式回退。"""

    def __init__(
        self,
        routes: Mapping[str, ModelRoute] | None = None,
    ) -> None:
        self.routes = dict(DEFAULT_ROUTES if routes is None else routes)

    def route(
        self,
        category: str,
        request: Mapping[str, Any] | None = None,
    ) -> ModelRoute:
        category = str(category or "").strip()
        if category not in TASK_CATEGORIES:
            raise ValueError(f"未注册的任务类别：{category or '<empty>'}")
        route = self.routes.get(category)
        if route is None:
            raise ValueError(f"任务类别缺少模型路由：{category}")
        return route


__all__ = [
    "DEFAULT_ROUTES",
    "ModelRoute",
    "ModelRouter",
    "PROPOSAL_SCHEMA",
    "RISK_LEVELS",
    "TASK_CATEGORIES",
    "classify_request",
]
