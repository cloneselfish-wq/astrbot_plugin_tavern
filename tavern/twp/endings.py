"""D1 结局条件核对与结算（纯函数层，D1_PLAN 03 RUN-014 / 18 §12、14）。

结局条件统一为注册运算符树：世界包在 ending 定义的 ``when`` 中声明
条件树（eq/ne/gt/gte/lt/lte/all/any/none/count/state_is/
capability_declared），由同一 Condition Engine（runtime.terminal_service
的 evaluate_condition_tree）执行，与命令前置、候选依赖、终局核对共用
一套算子与路径语义。

不再适配旧世界的 ``stable_nodes``、``signatories``、``testimonies``、
``recognized_nodes``、``price_accepted`` 等专属字段；无 ``when`` 的世界
仅回退到世界无关的通用 requires 核对（facts/clocks/vote）。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..contracts.actor_fate import declared_capabilities

# 结局条件允许的投影根：终局（terminal_conditions）只允许 session/party，
# 结局核对额外允许世界侧投票、时钟、场景、证据与知识投影。
ENDING_PATH_ROOTS = (
    "session",
    "party",
    "vote",
    "clock",
    "scene",
    "evidence",
    "knowledge",
    "faction",
    "ending",
)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def ending_definitions(world: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """读取世界包结局定义（rules.endings 或规则 ending 模块）。

    D1 标准作者源为数组（每项含稳定 ``id``）；旧式映射（id → 定义）仍可读。
    """

    rules = _mapping(world.get("rules"))
    endings = rules.get("endings")
    if not isinstance(endings, (Mapping, Sequence)) or isinstance(
        endings, (str, bytes)
    ):
        endings = _mapping(rules.get("ending")).get("endings")
    if isinstance(endings, Mapping):
        return {
            str(key): dict(value)
            for key, value in endings.items()
            if isinstance(value, Mapping)
        }
    result: dict[str, dict[str, Any]] = {}
    for item in _sequence(endings):
        if not isinstance(item, Mapping):
            continue
        ending_id = str(item.get("id") or "").strip()
        if ending_id:
            result[ending_id] = dict(item)
    return result


def build_ending_context(
    runtime: Mapping[str, Any],
    *,
    world: Mapping[str, Any] | None = None,
    session: Mapping[str, Any] | None = None,
    party: Mapping[str, Any] | None = None,
    vote: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """构建结局条件求值上下文（会话/队伍由宿主注入，其余取自运行态投影）。"""

    session_ctx = dict(session or {})
    runtime_session = runtime.get("session")
    if isinstance(runtime_session, Mapping):
        session_ctx.update(dict(runtime_session))
    party_ctx = dict(party or {})
    runtime_party = runtime.get("party")
    if isinstance(runtime_party, Mapping):
        party_ctx.update(dict(runtime_party))
    return {
        "session": session_ctx,
        "party": party_ctx,
        "vote": (
            dict(vote)
            if isinstance(vote, Mapping)
            else _mapping(runtime.get("vote"))
        ),
        "clock": _mapping(runtime.get("clock") or runtime.get("clocks")),
        "scene": _mapping(runtime.get("scene")),
        "evidence": _mapping(runtime.get("evidence")),
        "knowledge": _mapping(runtime.get("knowledge")),
        "faction": _mapping(runtime.get("factions") or runtime.get("faction")),
        "ending": _mapping(runtime.get("ending")),
        "capabilities": declared_capabilities(world or {}),
    }


def _revealed_facts(runtime: Mapping[str, Any]) -> set[str]:
    knowledge = _mapping(runtime.get("knowledge"))
    return {str(item) for item in _sequence(knowledge.get("revealed"))}


def _fact_gaps(
    runtime: Mapping[str, Any],
    requires: Mapping[str, Any],
) -> list[str]:
    revealed = _revealed_facts(runtime)
    gaps: list[str] = []
    for fact_id in _sequence(requires.get("facts")):
        if str(fact_id) and str(fact_id) not in revealed:
            gaps.append(f"未揭示关键事实 {fact_id}")
    return gaps


def _generic_requires_gap(
    runtime: Mapping[str, Any],
    requires: Mapping[str, Any],
) -> tuple[bool, list[str]]:
    """无 when 条件树的旧式结局回退：只核对世界无关的 facts/clocks/vote。

    stable_nodes_min/signatories_min/testimonies_min/
    recognized_nodes_min/price_accepted 等旧世界专属字段不再参与核对。
    """

    missing = _fact_gaps(runtime, requires)
    for clock_rule in _sequence(requires.get("clocks")):
        if not isinstance(clock_rule, Mapping):
            continue
        faction_id = str(clock_rule.get("faction") or "")
        clock_id = str(clock_rule.get("clock") or "")
        clocks_root = _mapping(runtime.get("clock") or runtime.get("clocks"))
        root_clock = clocks_root.get(clock_id) or clocks_root.get(
            f"clock:{clock_id}"
        )
        root_clock = _mapping(root_clock)
        value = root_clock.get("value")
        if value is None:
            factions = _mapping(runtime.get("factions"))
            faction = _mapping(factions.get(faction_id))
            clock = _mapping(_mapping(faction.get("clocks")).get(clock_id))
            value = clock.get("value")
        if value is None:
            missing.append(f"时钟 {clock_id} 未初始化")
            continue
        maximum = clock_rule.get("max")
        minimum = clock_rule.get("min")
        if maximum is not None and _int(value) >= _int(maximum):
            missing.append(f"时钟 {clock_id} 已填满（{value}/{maximum}）")
        elif minimum is not None and _int(value) < _int(minimum):
            missing.append(f"时钟 {clock_id} 不足（{value}/{minimum}）")
    vote_choice = str(requires.get("vote") or "").strip()
    if vote_choice:
        choice = str(
            _mapping(runtime.get("vote")).get("choice")
            or _mapping(runtime.get("ending")).get("choice")
            or ""
        )
        if choice != vote_choice:
            missing.append(f"表决选择不符（需要「{vote_choice}」）")
    if requires.get("vote_passed") and not bool(
        _mapping(runtime.get("ending")).get("vote_passed")
    ):
        missing.append("队伍投票未通过")
    return (not missing), missing


def _path_lookup(context: Mapping[str, Any], path: str) -> tuple[Any, bool]:
    current: Any = context
    for segment in (item for item in str(path or "").split(".") if item):
        if not isinstance(current, Mapping) or segment not in current:
            return None, False
        current = current[segment]
    return current, True


def _exists_leaf(
    node: Mapping[str, Any],
    context: Mapping[str, Any],
    reads: list[dict[str, Any]],
) -> bool:
    """结局侧 exists 叶运算符：只判断投影键是否存在，不比较值。"""

    path = str(node.get("path") or "").strip()
    value, found = _path_lookup(context, path)
    wanted = bool(node.get("value", True))
    reads.append({"path": path, "op": "exists", "found": found})
    return found == wanted


def _normalize_ending_tree(node: Any) -> Any:
    """把紧凑条件形式归一化为 op/value 叶形式。

    支持两种世界声明：
    - 终局风格：{"path": ..., "op": "gte", "value": 8}；
    - 紧凑风格：{"path": ..., "gte": 8} 与
      {"path": ..., "exists": true}（exists 为结局侧扩展运算符）。
    """

    if isinstance(node, Mapping):
        if "path" in node and not str(node.get("op") or "").strip():
            for key, value in list(node.items()):
                if key in {"path", "op", "value", "rel", "mode"}:
                    continue
                if key in {
                    "eq",
                    "ne",
                    "gt",
                    "gte",
                    "lt",
                    "lte",
                    "count",
                    "state_is",
                    "exists",
                }:
                    return {
                        "path": str(node.get("path") or ""),
                        "op": key,
                        "value": value,
                    }
            return node
        if any(key in node for key in ("all", "any", "none")):
            return {
                key: [
                    _normalize_ending_tree(child)
                    for child in _sequence(node.get(key))
                ]
                for key in ("all", "any", "none")
                if key in node
            }
    return node


def ending_readiness(
    runtime: Mapping[str, Any],
    world: Mapping[str, Any],
    *,
    session: Mapping[str, Any] | None = None,
    party: Mapping[str, Any] | None = None,
    vote: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """对每个结局输出 {label, met, missing[]} 清单。

    有 ``when`` 条件树的结局以树为准（缺失投影按未满足处理，不误报）；
    ``requires`` 中的事实缺口只进入 missing 说明，不改变树结论。
    """

    definitions = ending_definitions(world)
    context = build_ending_context(
        runtime,
        world=world,
        session=session,
        party=party,
        vote=vote,
    )
    result: dict[str, Any] = {"endings": {}, "summary": {"met": [], "pending": []}}
    for ending_id, definition in definitions.items():
        label = str(definition.get("label") or ending_id)
        requires = _mapping(definition.get("requires"))
        when = definition.get("when")
        if not (isinstance(when, Mapping) and when):
            when = definition.get("conditions")
        if isinstance(when, Mapping) and when:
            # 延迟导入：contracts.actor_fate 在模块级引用本模块，
            # 顶层导入会形成 import 环。
            from ..runtime.terminal_service import evaluate_condition_tree

            evaluation = evaluate_condition_tree(
                _normalize_ending_tree(when),
                context,
                allowed_roots=ENDING_PATH_ROOTS,
                extra_operators={"exists": _exists_leaf},
            )
            met = bool(evaluation.get("matched"))
            missing = _fact_gaps(runtime, requires)
            if not met:
                problem = evaluation.get("problem")
                if problem:
                    missing.insert(
                        0, str(problem.get("message") or "结局条件未满足")
                    )
                else:
                    missing.insert(0, "结局条件未满足")
        else:
            # D1 结局契约以条件树为权威；只声明旧式 requires 的世界
            # 一律按未满足处理（fail-safe），避免把无法核对的世界专属
            # 字段（stable_nodes/signatories 等）误判为已满足。
            missing = _generic_requires_gap(runtime, requires)[1]
            missing.insert(0, "结局未声明条件树（when/conditions）")
            met = False
        result["endings"][ending_id] = {
            "label": label,
            "met": bool(met),
            "missing": missing,
        }
        (result["summary"]["met"] if met else result["summary"]["pending"]).append(
            ending_id
        )
    return result


__all__ = [
    "ENDING_PATH_ROOTS",
    "build_ending_context",
    "ending_definitions",
    "ending_readiness",
]
