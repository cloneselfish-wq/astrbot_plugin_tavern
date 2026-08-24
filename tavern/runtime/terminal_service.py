"""D1 终局条件求值与仲裁纯服务（terminal_conditions@1.0）。

世界包在 rules.terminal_conditions 声明条件树；本模块用注册运算符
（eq/ne/gt/gte/lt/lte/all/any/none/count/state_is/capability_declared）
在会话与队伍投影上求值，并按确定性规则仲裁命中条件。

求值失败时按“不触发”处理（fail-safe）：缺失投影路径、未注册运算符、
非法节点都不得让副本意外终局。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..contracts.actor_fate import (
    ALLOWED_PATH_ROOTS,
    LOGIC_OPERATORS,
    PARTY_PROTECTED_PATHS,
    REGISTERED_OPERATORS,
    collect_condition_paths,
    condition_has_member_guard,
    declared_capabilities,
)
from .models import TerminalMatch

# 终止类型仲裁层级（同优先级时）：失败 > 正常完成 > 强制终止。
_TERMINATION_RANK = {"failed": 2, "completed": 1, "aborted": 0}
_LEAF_COMPARE_OPERATORS = frozenset({"eq", "ne", "gt", "gte", "lt", "lte"})


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def build_terminal_context(
    *,
    world: Mapping[str, Any] | None = None,
    session: Mapping[str, Any] | None = None,
    party: Mapping[str, Any] | None = None,
    capabilities: Sequence[str] | set[str] | None = None,
) -> dict[str, Any]:
    """构建终局条件求值上下文（只含允许的会话/队伍投影与能力声明）。"""

    declared = (
        {str(item) for item in capabilities}
        if capabilities is not None
        else declared_capabilities(world or {})
    )
    return {
        "session": dict(session or {}),
        "party": dict(party or {}),
        "capabilities": declared,
    }


def _path_value(
    context: Mapping[str, Any],
    path: str,
) -> tuple[Any, bool]:
    current: Any = context
    for segment in (item for item in str(path or "").split(".") if item):
        if not isinstance(current, Mapping) or segment not in current:
            return None, False
        current = current[segment]
    return current, True


def _compare_values(left: Any, operator: str, right: Any) -> bool:
    if operator == "eq":
        return left == right
    if operator == "ne":
        return left != right
    if operator in {"gt", "gte", "lt", "lte"}:
        if isinstance(left, bool) or isinstance(right, bool):
            raise TypeError("布尔值不能参与数值大小比较")
        try:
            a, b = float(left), float(right)
        except (TypeError, ValueError, OverflowError) as exc:
            raise TypeError("大小比较的双方必须是数值") from exc
        return {
            "gt": a > b,
            "gte": a >= b,
            "lt": a < b,
            "lte": a <= b,
        }[operator]
    raise ValueError(f"不支持的条件运算符：{operator}")


def _member_state(member: Mapping[str, Any]) -> str:
    fate = member.get("fate")
    if isinstance(fate, Mapping):
        return str(fate.get("state") or "")
    return str(member.get("state") or "")


def _evaluate_leaf(
    node: Mapping[str, Any],
    context: Mapping[str, Any],
    reads: list[dict[str, Any]],
    allowed_roots: Sequence[str],
    extra_operators: Mapping[str, Any] | None = None,
) -> bool:
    op = str(node.get("op") or "").lower()
    if op == "capability_declared":
        capability = str(node.get("value") or node.get("capability") or "").strip()
        declared = context.get("capabilities", set())
        matched = bool(declared and capability in declared)
        reads.append({"op": "capability_declared", "value": capability, "matched": matched})
        return matched
    if extra_operators and op in extra_operators:
        # 结局侧可注册额外叶运算符（如 exists）；终局默认不启用。
        return bool(extra_operators[op](node, context, reads))
    path = str(node.get("path") or "").strip()
    if not path.startswith(tuple(allowed_roots)):
        raise ValueError(f"终局条件引用了未允许的投影路径 {path}")
    value, found = _path_value(context, path)
    reads.append({"path": path, "op": op, "found": found, "value": value})
    if not found:
        # 缺失投影按不匹配处理（fail-safe），避免副本意外终局。
        return False
    if op in _LEAF_COMPARE_OPERATORS:
        return _compare_values(value, op, node.get("value"))
    if op == "count":
        raw = _sequence(value)
        count = len(raw)
        comparator = node.get("value")
        if isinstance(comparator, Mapping):
            rel_map = _mapping(comparator)
            rel_op = next(iter(rel_map))
            return _compare_values(count, rel_op, rel_map[rel_op])
        return _compare_values(count, "eq", comparator)
    if op == "state_is":
        mode = str(node.get("mode") or "any").lower()
        target = str(node.get("value") or "").strip()
        states = [_member_state(item) for item in _sequence(value)]
        if mode == "all":
            return bool(states) and all(state == target for state in states)
        return any(state == target for state in states)
    raise ValueError(f"不支持的终局条件运算符：{op}")


def _empty_party_guard(node: Any, context: Mapping[str, Any]) -> bool:
    """空队伍保护：队伍为空且条件引用生死计数但没有显式非空保护时强制不触发。"""

    party = _mapping(context.get("party"))
    if int(party.get("member_count", 0) or 0) > 0:
        return False
    paths = collect_condition_paths(node)
    if not (paths & PARTY_PROTECTED_PATHS):
        return False
    return not condition_has_member_guard(node)


def evaluate_condition_tree(
    node: Any,
    context: Mapping[str, Any],
    *,
    allowed_roots: Sequence[str] = ALLOWED_PATH_ROOTS,
    extra_operators: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """求值条件树，返回 matched/reads/problem（失败按不触发处理）。

    allowed_roots 允许结局核对复用同一引擎并放宽投影根
    （D1_PLAN 03 RUN-014 / 18 §12）；终局默认仍只允许
    session/party 投影根。
    """

    reads: list[dict[str, Any]] = []

    def visit(item: Any, depth: int) -> bool:
        if depth > 16:
            raise ValueError("终局条件嵌套超过安全上限")
        if item is None or item == {}:
            # 终局条件按 fail-safe 处理：空节点不触发，避免副本意外终局。
            return False
        if isinstance(item, bool):
            return item
        if not isinstance(item, Mapping):
            raise TypeError("终局条件节点必须是对象")
        for logic in ("all", "any", "none"):
            if logic in item:
                values = _sequence(item.get(logic))
                results = [visit(child, depth + 1) for child in values]
                if logic == "all":
                    return all(results)
                if logic == "any":
                    return any(results)
                return not any(results)
        return _evaluate_leaf(
            item,
            context,
            reads,
            tuple(allowed_roots),
            extra_operators,
        )

    try:
        if _empty_party_guard(node, context):
            return {
                "matched": False,
                "reads": reads,
                "problem": None,
                "guarded": True,
                "blocked_reason": "empty_party_guard",
            }
        matched = visit(node, 0)
    except (KeyError, TypeError, ValueError) as exc:
        return {
            "matched": False,
            "reads": reads,
            "problem": {
                "code": "invalid_terminal_condition",
                "message": str(exc),
            },
            "guarded": False,
            "blocked_reason": "",
        }
    return {
        "matched": bool(matched),
        "reads": reads,
        "problem": None,
        "guarded": False,
        "blocked_reason": "",
    }


def _elimination_condition(node: Any) -> bool:
    """条件是否属于“队伍全灭”类不可逆终局（living_count 归零）。

    必须递归进入 all/any/none 的列表元素：雾港等世界把
    living_count=0 放在 all 分支中，只递归 Mapping 值会漏检，
    导致全灭终局失去不可逆优先级加成（D1_PLAN 18 §7.2）。
    """

    if isinstance(node, Mapping):
        if (
            str(node.get("path") or "") == "party.living_count"
            and str(node.get("op") or "").lower() == "eq"
            and str(node.get("value")) in {"0", "0.0", "False", "false"}
        ):
            return True
        return any(_elimination_condition(value) for value in node.values())
    if isinstance(node, (list, tuple)):
        return any(_elimination_condition(value) for value in node)
    return False


def evaluate_terminal_conditions(
    conditions: Sequence[Mapping[str, Any]],
    context: Mapping[str, Any],
) -> list[TerminalMatch]:
    """求值全部终局条件，返回稳定排序的匹配清单（未命中也保留，便于诊断）。"""

    matches: list[TerminalMatch] = []
    for condition in conditions:
        when = _mapping(condition.get("when"))
        result = evaluate_condition_tree(when, context)
        matches.append(
            TerminalMatch(
                condition_id=str(condition.get("id") or ""),
                label=str(condition.get("label") or ""),
                matched=bool(result["matched"]),
                priority=int(condition.get("priority", 0) or 0),
                termination_type=str(
                    condition.get("termination_type") or "completed"
                ).lower(),
                ending_ref=str(condition.get("ending_ref") or ""),
                archive_policy=str(
                    condition.get("archive_policy") or "automatic_readonly"
                ).lower(),
                reason=str(condition.get("reason") or ""),
                elimination=_elimination_condition(when),
                blocked_reason=str(result.get("blocked_reason") or ""),
                reads=tuple(
                    dict(item)
                    for item in _sequence(result.get("reads"))
                ),
            )
        )
    return sorted(
        matches,
        key=lambda item: (
            -item.priority,
            item.condition_id,
        ),
    )


def arbitrate_terminal_conditions(
    matches: Sequence[Mapping[str, Any]] | Sequence[TerminalMatch],
) -> dict[str, Any] | None:
    """确定性仲裁：优先最高 priority，再按终止类型，最后按 id 稳定排序。

    规则（D1_PLAN 18 §7.2）：不可逆全灭终局 > 世界明确失败终局 >
    正常结局 > 普通场景转移。世界通过 priority 表达层级；同优先级时
    failed > completed > aborted；仍未区分时按条件 id 字典序保证
    两个后台任务选到同一个赢家。
    """

    matched = [
        item.to_dict() if isinstance(item, TerminalMatch) else dict(item)
        for item in matches
        if bool(item.matched if isinstance(item, TerminalMatch) else item.get("matched"))
    ]
    if not matched:
        return None
    ranked = sorted(
        matched,
        key=lambda item: (
            -int(item.get("priority", 0) or 0),
            -int(bool(item.get("elimination"))),
            -_TERMINATION_RANK.get(
                str(item.get("termination_type") or "").lower(), -1
            ),
            str(item.get("condition_id") or ""),
        ),
    )
    return ranked[0]


def terminal_match_dicts(
    matches: Sequence[TerminalMatch],
) -> list[dict[str, Any]]:
    return [item.to_dict() for item in matches]


__all__ = [
    "arbitrate_terminal_conditions",
    "build_terminal_context",
    "evaluate_condition_tree",
    "evaluate_terminal_conditions",
    "terminal_match_dicts",
]
