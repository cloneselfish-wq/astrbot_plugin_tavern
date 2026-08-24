"""D1 actor_fate@1.0 与 terminal_conditions@1.0 世界契约（纯函数层）。

世界包通过 ``rules.actor_fate`` 与 ``rules.terminal_conditions`` 声明角色命运
状态机、救援窗口与自动终局条件。宿主只消费本模块归一化后的规范结构，不读取
世界包私有字段；世界包也不能声明宿主函数。

模块能力版本随 原子切换为
actor_fate@1.0.0-rc10 与 terminal_conditions@1.0.0-rc10，
不构成 TWP 2.0。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

ACTOR_FATE_CAPABILITY = "actor_fate@1.0.0-rc10"
TERMINAL_CONDITIONS_CAPABILITY = "terminal_conditions@1.0.0-rc10"

TERMINATION_TYPES = frozenset({"completed", "failed", "aborted"})
ARCHIVE_POLICIES = frozenset(
    {
        "manual",
        "automatic",
        "automatic_readonly",
        "automatic_failed_readonly",
    }
)
FATE_TRANSITION_EFFECT = "actor_fate.transition"

# 终局条件只能使用注册运算符（D1_PLAN 18 §7.3）。
REGISTERED_OPERATORS = frozenset(
    {
        "eq",
        "ne",
        "gt",
        "gte",
        "lt",
        "lte",
        "all",
        "any",
        "none",
        "count",
        "state_is",
        "capability_declared",
    }
)
LOGIC_OPERATORS = frozenset({"all", "any", "none"})
LEAF_OPERATORS = REGISTERED_OPERATORS - LOGIC_OPERATORS

# 允许的投影路径根（D1_PLAN 18 §7.3）：终局只能引用会话与队伍投影。
ALLOWED_PATH_ROOTS = ("session", "party")

# 引用队伍生死计数的条件必须同时声明“队伍非空”保护，
# 空队伍不得触发 living_count = 0 终局（D1_PLAN 18 §6）。
PARTY_PROTECTED_PATHS = frozenset(
    {
        "party.living_count",
        "party.dead_count",
        "party.incapacitated_count",
        "party.members",
    }
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


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"true", "yes", "1"}


def module_data(world: Mapping[str, Any], key: str, default: Any = None) -> Any:
    """读取世界包模块数据：优先顶层字段，其次 rules.<key>。"""

    source = _mapping(world)
    if key in source:
        return source[key]
    rules = source.get("rules")
    if isinstance(rules, Mapping) and key in rules:
        return rules[key]
    return default


def declared_capabilities(world: Mapping[str, Any]) -> set[str]:
    """收集世界包显式声明的能力名（忽略版本约束后缀）。"""

    names: set[str] = set()

    def add(value: Any) -> None:
        if isinstance(value, str):
            base = value.split("@", 1)[0].strip()
            if base:
                names.add(base)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for item in value:
                add(item)

    add(world.get("capabilities"))
    add(world.get("required_features"))
    rules = _mapping(world.get("rules"))
    capabilities = rules.get("capabilities")
    if isinstance(capabilities, Mapping):
        add(capabilities.get("capabilities"))
        add(capabilities.get("declared"))
    index = rules.get("capability_index")
    if isinstance(index, Mapping):
        add(list(index.keys()))
    return names


def _normalize_state(raw: Mapping[str, Any], index: int) -> dict[str, Any]:
    state_id = str(raw.get("id") or raw.get("state_id") or "").strip()
    return {
        "id": state_id,
        "label": str(raw.get("label") or "").strip(),
        "terminal": _bool(raw.get("terminal")),
        "can_act": _bool(raw.get("can_act"), True),
        "initial": _bool(raw.get("initial")) or (index == 0),
        "message": str(raw.get("message") or "").strip(),
    }


def _normalize_transition(raw: Mapping[str, Any]) -> dict[str, Any]:
    effect = str(raw.get("effect") or FATE_TRANSITION_EFFECT).strip()
    rescue = raw.get("opens_rescue_window")
    rescue_kind = ""
    if isinstance(rescue, str):
        rescue_kind = rescue.strip()
        rescue = True
    elif rescue is None:
        rescue_kind = str(raw.get("rescue_window_kind") or "").strip()
    elif rescue:
        # 布尔开关 + 独立 kind 字段：两种写法都必须归一化出窗口 kind，
        # 否则构建门禁无法核验转换引用的救援窗口。
        rescue_kind = str(raw.get("rescue_window_kind") or "").strip()
    return {
        "from": str(raw.get("from") or "").strip(),
        "to": str(raw.get("to") or "").strip(),
        "severity": str(raw.get("severity") or "").strip().lower(),
        "cause": str(raw.get("cause") or "").strip().lower(),
        "effect": effect or FATE_TRANSITION_EFFECT,
        "reason_required": _bool(raw.get("reason_required")),
        "reversible": _bool(raw.get("reversible")),
        "opens_rescue_window": _bool(rescue),
        "rescue_window_kind": rescue_kind,
        "consumes_protection_resource": str(
            raw.get("consumes_protection_resource") or ""
        ).strip(),
    }


def _normalize_rescue_window(raw: Mapping[str, Any]) -> dict[str, Any]:
    commands = [
        str(item).strip()
        for item in _sequence(raw.get("allowed_rescue_commands"))
        if str(item).strip()
    ]
    success_commands = [
        str(item).strip()
        for item in _sequence(raw.get("success_commands"))
        if str(item).strip()
    ]
    failure_commands = [
        str(item).strip()
        for item in _sequence(raw.get("failure_commands"))
        if str(item).strip()
    ]
    labels = _mapping(raw.get("command_labels"))
    return {
        "kind": str(raw.get("kind") or raw.get("window_kind") or "default").strip(),
        "allowed_rescue_commands": commands,
        "success_commands": success_commands or commands,
        "failure_commands": failure_commands,
        "command_labels": {
            str(key): str(value)
            for key, value in labels.items()
            if str(key).strip()
        },
        "expires_on": str(raw.get("expires_on") or "").strip(),
        "success_transition": _transition_pair(raw.get("success_transition")),
        "failure_transition": _transition_pair(raw.get("failure_transition")),
    }


def _transition_pair(value: Any) -> tuple[str, str]:
    """把 {from,to} 或 'critical->wounded' 归一化为 (from, to)。"""

    if isinstance(value, Mapping):
        return (
            str(value.get("from") or "").strip(),
            str(value.get("to") or "").strip(),
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        items = list(value)
        if len(items) == 2:
            return str(items[0]).strip(), str(items[1]).strip()
        return "", ""
    text = str(value or "").strip()
    if "->" in text:
        left, right = text.split("->", 1)
        return left.strip(), right.strip()
    if "→" in text:
        left, right = text.split("→", 1)
        return left.strip(), right.strip()
    return "", ""


def parse_actor_fate(world: Mapping[str, Any]) -> dict[str, Any]:
    """归一化 rules.actor_fate 为宿主可消费的规范结构。"""

    raw = module_data(world, "actor_fate")
    if not isinstance(raw, Mapping) or not raw:
        return {
            "declared": False,
            "capability": ACTOR_FATE_CAPABILITY,
            "states": [],
            "transitions": [],
            "initial_state": "",
            "protection_resources": [],
            "rescue_windows": [],
            "membership_filter": {},
            "consequence_map": {},
            "policy": {
                "lethal_preview_required": True,
                "direct_terminal_authorized": False,
            },
        }
    states = [
        _normalize_state(item, index)
        for index, item in enumerate(_sequence(raw.get("states")))
        if isinstance(item, Mapping)
    ]
    transitions = [
        _normalize_transition(item)
        for item in _sequence(raw.get("transitions"))
        if isinstance(item, Mapping)
    ]
    protection = [
        {
            "id": str(item.get("id") or item.get("resource_id") or "").strip(),
            "label": str(item.get("label") or "").strip(),
            "consumed_by": str(item.get("consumed_by") or "").strip(),
        }
        for item in _sequence(raw.get("protection_resources"))
        if isinstance(item, Mapping)
    ]
    rescue_windows = [
        _normalize_rescue_window(item)
        for item in _sequence(raw.get("rescue_windows"))
        if isinstance(item, Mapping)
    ]
    membership = raw.get("membership_filter")
    membership = membership if isinstance(membership, Mapping) else {}
    initial_states = [state["id"] for state in states if state["initial"]]
    initial_state = initial_states[0] if initial_states else (
        states[0]["id"] if states else ""
    )
    consequence_map = raw.get("consequence_map")
    consequence_map = consequence_map if isinstance(consequence_map, Mapping) else {}
    policy = raw.get("policy")
    policy = policy if isinstance(policy, Mapping) else {}
    return {
        "declared": True,
        "capability": ACTOR_FATE_CAPABILITY,
        "states": states,
        "transitions": transitions,
        "initial_state": initial_state,
        "protection_resources": protection,
        "rescue_windows": rescue_windows,
        "membership_filter": dict(membership),
        "consequence_map": {
            str(key): str(value)
            for key, value in consequence_map.items()
            if str(key).strip()
        },
        "policy": {
            "lethal_preview_required": _bool(
                policy.get("lethal_preview_required"),
                True,
            ),
            # This is deliberately opt-in world authority.  A host/model
            # payload cannot set it because only the frozen world contract is
            # normalized here.
            "direct_terminal_authorized": _bool(
                policy.get("direct_terminal_authorized")
                or policy.get("allow_direct_terminal_consequence"),
                False,
            ),
        },
    }


def validate_actor_fate(contract: Mapping[str, Any]) -> list[str]:
    """构建门禁校验，返回中文问题清单（空列表表示通过）。"""

    issues: list[str] = []
    if not bool(contract.get("declared")):
        return issues
    states = _sequence(contract.get("states"))
    transitions = _sequence(contract.get("transitions"))
    if not states:
        issues.append("actor_fate 未声明任何状态")
        return issues
    state_ids = [str(state.get("id") or "") for state in states]
    state_by_id = {state_id: state for state_id, state in zip(state_ids, states)}
    if len(state_ids) != len(set(state_ids)):
        issues.append("actor_fate 状态 id 重复")
    for state in states:
        if not str(state.get("id") or ""):
            issues.append("actor_fate 状态缺少 id")
        if not str(state.get("label") or ""):
            issues.append(f"状态 {state.get('id') or '<无 id>'} 缺少中文 label")
    initial = [state for state in states if bool(state.get("initial"))]
    if len(initial) > 1:
        issues.append("actor_fate 初始状态必须唯一")
    if not initial and not str(contract.get("initial_state") or ""):
        issues.append("actor_fate 必须声明唯一初始状态")
    rescue_kinds = {
        str(window.get("kind") or "")
        for window in _sequence(contract.get("rescue_windows"))
    }
    protection_ids = {
        str(item.get("id") or "")
        for item in _sequence(contract.get("protection_resources"))
    }
    for transition in transitions:
        from_state = str(transition.get("from") or "")
        to_state = str(transition.get("to") or "")
        if from_state not in state_by_id:
            issues.append(f"转换引用了未声明状态 {from_state or '<空>'}")
        if to_state not in state_by_id:
            issues.append(f"转换 {from_state}→{to_state} 引用了未声明状态")
        if from_state and from_state == to_state:
            issues.append(f"转换 {from_state}→{to_state} 不能原地自转")
        if str(transition.get("effect") or "") != FATE_TRANSITION_EFFECT:
            issues.append(
                f"转换 {from_state}→{to_state} 使用了未注册效果"
                f" {transition.get('effect') or '<空>'}"
            )
        if bool(transition.get("opens_rescue_window")):
            kind = str(transition.get("rescue_window_kind") or "default")
            # 无条件校验：声明开启救援窗口的转换必须引用已声明的窗口，
            # 即使世界完全没有声明任何救援窗口也视为非法。
            if kind not in rescue_kinds:
                issues.append(f"转换 {from_state}→{to_state} 引用了未声明的救援窗口 {kind}")
        consumed = str(transition.get("consumes_protection_resource") or "")
        if consumed and protection_ids and consumed not in protection_ids:
            issues.append(f"转换 {from_state}→{to_state} 引用了未声明的保护资源 {consumed}")
        if from_state in state_by_id and bool(state_by_id[from_state].get("terminal")):
            issues.append(f"终态 {from_state} 不可作为转换起点")
    for window in _sequence(contract.get("rescue_windows")):
        success = _transition_pair(window.get("success_transition"))
        failure = _transition_pair(window.get("failure_transition"))
        for label, pair in (("成功", success), ("失败", failure)):
            if not pair[0] or not pair[1]:
                issues.append(f"救援窗口 {window.get('kind') or '<无 kind>'} 缺少{label}转换")
                continue
            exists = any(
                str(item.get("from")) == pair[0] and str(item.get("to")) == pair[1]
                for item in transitions
            )
            if not exists:
                issues.append(
                    f"救援窗口 {window.get('kind') or '<无 kind>'} 的{label}转换"
                    f" {pair[0]}→{pair[1]} 未声明"
                )
    consequence_map = _mapping(contract.get("consequence_map"))
    for severity, target in consequence_map.items():
        target = str(target or "")
        if target not in state_by_id:
            issues.append(f"后果映射 {severity} 引用了未声明状态 {target or '<空>'}")
            continue
        if severity == "serious" and bool(state_by_id[target].get("terminal")):
            issues.append("普通后果（serious）不能直接映射到终态")
    return issues


def parse_terminal_conditions(
    world: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """归一化 rules.terminal_conditions 为条件清单。"""

    raw = module_data(world, "terminal_conditions")
    if isinstance(raw, Mapping):
        items = _sequence(raw.get("conditions")) or _sequence(raw.get("items"))
    else:
        items = _sequence(raw)
    conditions: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        when = item.get("when")
        when = when if isinstance(when, Mapping) else {}
        conditions.append(
            {
                "id": str(item.get("id") or "").strip(),
                "label": str(item.get("label") or "").strip(),
                "when": dict(when),
                "priority": _int(item.get("priority")),
                "ending_ref": str(item.get("ending_ref") or "").strip(),
                "termination_type": str(
                    item.get("termination_type") or "completed"
                ).strip().lower(),
                "archive_policy": str(
                    item.get("archive_policy") or "automatic_readonly"
                ).strip().lower(),
                "reason": str(item.get("reason") or "").strip(),
            }
        )
    return conditions


def collect_condition_paths(node: Any) -> set[str]:
    """收集条件树中引用的全部投影路径。"""

    paths: set[str] = set()
    if isinstance(node, Mapping):
        for key, value in node.items():
            if key == "path":
                paths.add(str(value or "").strip())
            else:
                paths.update(collect_condition_paths(value))
    elif isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
        for item in node:
            paths.update(collect_condition_paths(item))
    return paths


def condition_has_member_guard(node: Any) -> bool:
    """条件树是否包含 party.member_count > 0（或 ≥1）的非空队伍保护。"""

    if isinstance(node, Mapping):
        if str(node.get("path") or "") == "party.member_count":
            op = str(node.get("op") or "").lower()
            value = node.get("value")
            try:
                number = float(value)
            except (TypeError, ValueError):
                number = 0.0
            # gt 0 / gte 1 / ne 0 都表示“队伍必须非空”。
            if op == "gt" and number >= 0:
                return True
            if op == "gte" and number >= 1:
                return True
            if op == "ne" and number != 0:
                return True
        return any(condition_has_member_guard(value) for value in node.values())
    if isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
        return any(condition_has_member_guard(item) for item in node)
    return False


def validate_condition_tree(node: Any) -> list[str]:
    """校验终局条件树：运算符、路径根与字段形态。"""

    issues: list[str] = []

    def visit(item: Any) -> None:
        if item is None:
            return
        if not isinstance(item, Mapping):
            issues.append("终局条件节点必须是对象")
            return
        if "all" in item:
            values = item.get("all")
            if not isinstance(values, list):
                issues.append("all 必须是数组")
            else:
                for child in values:
                    visit(child)
            return
        if "any" in item:
            values = item.get("any")
            if not isinstance(values, list):
                issues.append("any 必须是数组")
            else:
                for child in values:
                    visit(child)
            return
        if "none" in item:
            values = item.get("none")
            if not isinstance(values, list):
                issues.append("none 必须是数组")
            else:
                for child in values:
                    visit(child)
            return
        op = str(item.get("op") or "").strip().lower()
        if not op:
            issues.append("终局条件叶子缺少 op")
            return
        if op not in REGISTERED_OPERATORS:
            issues.append(f"终局条件使用了未注册运算符 {op}")
            return
        if op == "capability_declared":
            capability = str(
                item.get("value") or item.get("capability") or ""
            ).strip()
            if not capability:
                issues.append("capability_declared 缺少能力名")
            return
        path = str(item.get("path") or "").strip()
        if not path:
            issues.append("终局条件叶子缺少 path")
            return
        allowed_keys = {"op", "path", "value", "rel", "mode"}
        unexpected = sorted(set(item.keys()) - allowed_keys)
        if unexpected:
            issues.append(f"终局条件叶子包含未注册字段：{', '.join(unexpected)}")
        if not path.startswith(ALLOWED_PATH_ROOTS):
            issues.append(f"终局条件引用了未允许的投影路径 {path}")
        if op in {"eq", "ne", "gt", "gte", "lt", "lte"} and "value" not in item:
            issues.append(f"运算符 {op} 缺少 value")
        if op == "count":
            value = item.get("value")
            if isinstance(value, Mapping):
                rel = _mapping(value)
                if len(rel) != 1:
                    issues.append("count 的比较对象必须只含一个比较运算符")
                for rel_op, _ in rel.items():
                    if rel_op not in {"eq", "ne", "gt", "gte", "lt", "lte"}:
                        issues.append(f"count 使用了未注册比较运算符 {rel_op}")
            elif not isinstance(value, (int, float)):
                issues.append("count 的 value 必须是数字或单个比较对象")
        if op == "state_is" and not str(item.get("value") or "").strip():
            issues.append("state_is 缺少目标状态")

    visit(node)
    return issues


def validate_terminal_conditions(
    conditions: Sequence[Mapping[str, Any]],
    world: Mapping[str, Any],
) -> list[str]:
    """终局条件构建门禁：引用、优先级、归档策略与条件树形态。"""

    # 延迟导入：twp.endings 在本模块顶部反向引用 declared_capabilities，
    # 顶层互相导入会造成循环导入（与加载顺序相关）。
    from ..twp.endings import ending_definitions

    issues: list[str] = []
    endings = ending_definitions(world)
    seen: set[str] = set()
    for condition in conditions:
        condition_id = str(condition.get("id") or "")
        if not condition_id:
            issues.append("终局条件缺少 id")
        elif condition_id in seen:
            issues.append(f"终局条件 id 重复：{condition_id}")
        seen.add(condition_id)
        if not str(condition.get("label") or ""):
            issues.append(f"终局条件 {condition_id or '<无 id>'} 缺少中文 label")
        termination_type = str(condition.get("termination_type") or "").lower()
        if termination_type not in TERMINATION_TYPES:
            issues.append(
                f"终局条件 {condition_id or '<无 id>'} 使用了未注册的终止类型"
                f" {termination_type or '<空>'}"
            )
        archive_policy = str(condition.get("archive_policy") or "").lower()
        if archive_policy not in ARCHIVE_POLICIES:
            issues.append(
                f"终局条件 {condition_id or '<无 id>'} 使用了未注册的归档策略"
                f" {archive_policy or '<空>'}"
            )
        ending_ref = str(condition.get("ending_ref") or "")
        if ending_ref and ending_ref not in endings:
            issues.append(
                f"终局条件 {condition_id or '<无 id>'} 引用了未声明的结局"
                f" {ending_ref}"
            )
        if termination_type in {"failed", "completed"} and not ending_ref:
            issues.append(
                f"终局条件 {condition_id or '<无 id>'} 必须引用世界结局"
            )
        if archive_policy in {"automatic", "automatic_readonly"} and not ending_ref:
            issues.append(
                f"终局条件 {condition_id or '<无 id>'} 的自动归档必须引用世界结局"
            )
        if archive_policy == "automatic_failed_readonly" and termination_type != "failed":
            issues.append(
                f"终局条件 {condition_id or '<无 id>'} 的失败归档只适用于 failed"
            )
        when = condition.get("when")
        if not isinstance(when, Mapping) or not when:
            issues.append(f"终局条件 {condition_id or '<无 id>'} 缺少 when 条件")
            continue
        issues.extend(validate_condition_tree(when))
        paths = collect_condition_paths(when)
        if paths & PARTY_PROTECTED_PATHS and not condition_has_member_guard(when):
            issues.append(
                f"终局条件 {condition_id or '<无 id>'} 引用了队伍生死计数"
                " 但缺少 party.member_count > 0 的非空队伍保护"
            )
    return issues


__all__ = [
    "ACTOR_FATE_CAPABILITY",
    "ALLOWED_PATH_ROOTS",
    "ARCHIVE_POLICIES",
    "FATE_TRANSITION_EFFECT",
    "LEAF_OPERATORS",
    "LOGIC_OPERATORS",
    "PARTY_PROTECTED_PATHS",
    "REGISTERED_OPERATORS",
    "TERMINAL_CONDITIONS_CAPABILITY",
    "TERMINATION_TYPES",
    "collect_condition_paths",
    "condition_has_member_guard",
    "declared_capabilities",
    "module_data",
    "parse_actor_fate",
    "parse_terminal_conditions",
    "validate_actor_fate",
    "validate_condition_tree",
    "validate_terminal_conditions",
]

