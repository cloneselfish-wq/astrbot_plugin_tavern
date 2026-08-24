"""D1 角色命运状态机纯服务（actor_fate@1.0）。

覆盖：合法状态转换、结构化后果结算、保护资源降级、救援窗口
（创建/完成/过期幂等）、队伍聚合与玩家可见命运投影。

本模块不读写数据库：宿主在事务内调用这些纯函数，并把记录、窗口
与聚合结果持久化。终态不可离开、普通失败不直接死亡、空队伍不触发
终局的约束在此强制执行。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..contracts.actor_fate import (
    FATE_TRANSITION_EFFECT,
    PARTY_PROTECTED_PATHS,
    parse_actor_fate,
)
from .models import FateRecord, PartySummary, RescueWindow

DEFAULT_MEMBERSHIP_FILTER: dict[str, Any] = {
    "role_types": ["player"],
    "participation_statuses": ["active", "standby", "away"],
    "card_statuses": ["approved"],
    "card_stages": ["core_ready", "staged_pending", "stage_locked", "complete"],
    "exclude_entity_kinds": ["npc", "summon", "proxy", "observer"],
    "require_confirmed": True,
}


class InvalidFateTransition(ValueError):
    """状态转换不合法（未声明、跨级或终态离开）。"""


class InvalidConsequence(ValueError):
    """结构化后果不合法（字段缺失或世界未声明对应转换）。"""


class InsufficientProtection(ValueError):
    """保护资源不足，无法承担本次后果。"""


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def state_definition(
    contract: Mapping[str, Any],
    state_id: str,
) -> dict[str, Any] | None:
    for state in _sequence(contract.get("states")):
        if str(state.get("id") or "") == state_id:
            return dict(state)
    return None


def is_terminal(contract: Mapping[str, Any], state_id: str) -> bool:
    state = state_definition(contract, state_id)
    return bool(state and state.get("terminal"))


def can_act(contract: Mapping[str, Any], state_id: str) -> bool:
    state = state_definition(contract, state_id)
    if state is None:
        return False
    return bool(state.get("can_act", not bool(state.get("terminal"))))


def transitions_from(
    contract: Mapping[str, Any],
    state_id: str,
) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in _sequence(contract.get("transitions"))
        if str(item.get("from") or "") == state_id
    ]


def find_transition(
    contract: Mapping[str, Any],
    from_state: str,
    to_state: str,
) -> dict[str, Any] | None:
    for item in _sequence(contract.get("transitions")):
        if (
            str(item.get("from") or "") == from_state
            and str(item.get("to") or "") == to_state
        ):
            return dict(item)
    return None


def consume_protection(
    protection: Mapping[str, int],
    resource_id: str,
) -> tuple[bool, dict[str, int]]:
    """消耗 1 单位保护资源；不足时不改动并返回 False。"""

    current = int(protection.get(resource_id, 0) or 0)
    if current <= 0:
        return False, dict(protection)
    updated = dict(protection)
    updated[resource_id] = current - 1
    return True, updated


def apply_transition(
    *,
    contract: Mapping[str, Any],
    actor_ref: str,
    from_state: str,
    to_state: str,
    transition: Mapping[str, Any] | None = None,
    reason: str = "",
    source: str = "",
    sequence: int = 0,
    created_at: str = "",
    event_ref: str = "",
    protection: Mapping[str, int] | None = None,
) -> FateRecord:
    """执行一次合法命运转换并返回记录；非法转换直接抛错。"""

    actor_ref = str(actor_ref or "").strip()
    if not actor_ref:
        raise InvalidFateTransition("命运转换必须指定角色")
    if state_definition(contract, from_state) is None:
        raise InvalidFateTransition(f"未知的当前状态：{from_state or '<空>'}")
    if state_definition(contract, to_state) is None:
        raise InvalidFateTransition(f"未知的目标状态：{to_state or '<空>'}")
    if is_terminal(contract, from_state):
        raise InvalidFateTransition("终态不可作为转换起点，永久终局结果不可回退")
    transition = (
        dict(transition)
        if transition is not None
        else find_transition(contract, from_state, to_state)
    )
    if transition is None:
        raise InvalidFateTransition(
            f"世界未声明状态转换 {from_state}→{to_state}"
        )
    if transition.get("effect") != FATE_TRANSITION_EFFECT:
        raise InvalidFateTransition(
            f"状态转换 {from_state}→{to_state} 使用未注册效果"
        )
    if bool(transition.get("reason_required")) and not str(reason or "").strip():
        raise InvalidFateTransition(
            f"状态转换 {from_state}→{to_state} 必须填写原因"
        )
    consumed = str(transition.get("consumes_protection_resource") or "")
    if consumed:
        available = int((protection or {}).get(consumed, 0) or 0)
        if available <= 0:
            raise InsufficientProtection(
                f"保护资源不足：{consumed}"
            )
    rescue_kind = str(transition.get("rescue_window_kind") or "")
    return FateRecord(
        actor_ref=actor_ref,
        from_state=from_state,
        to_state=to_state,
        reason=str(reason or "").strip(),
        source=str(source or "").strip(),
        reversible=bool(transition.get("reversible")),
        opens_rescue_window=bool(transition.get("opens_rescue_window")),
        rescue_window_kind=rescue_kind,
        consumed_protection_resource=consumed,
        sequence=int(sequence or 0),
        created_at=str(created_at or ""),
        event_ref=str(event_ref or ""),
    )


def _consequence_target(
    contract: Mapping[str, Any],
    current_state: str,
    severity: str,
    rescue_window: bool,
) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
    """按世界转换表计算后果目标状态与具体转换（不硬编码任何世界状态名）。"""

    states = {str(item.get("id") or ""): item for item in _sequence(contract.get("states"))}
    explicit = str((contract.get("consequence_map") or {}).get(severity) or "")
    explicit_transition = (
        find_transition(contract, current_state, explicit)
        if explicit
        else None
    )
    if explicit_transition is not None:
        target = states.get(explicit)
        if target is None:
            raise InvalidConsequence(f"后果映射引用了未知状态 {explicit}")
        if severity == "serious":
            if bool(target.get("terminal")):
                raise InvalidConsequence("普通后果不能直接进入终态")
            return explicit, target, explicit_transition
        if severity == "lethal" and rescue_window:
            # 救援路径的目标状态必须是开启救援窗口的转换。
            if not bool(explicit_transition.get("opens_rescue_window")):
                raise InvalidConsequence("救援路径必须通过开启救援窗口的转换")
            if bool(target.get("terminal")):
                raise InvalidConsequence("救援路径不能直接进入终态")
            return explicit, target, explicit_transition
        if severity == "lethal" and not rescue_window and bool(target.get("terminal")):
            return explicit, target, explicit_transition
        # 未请求救援的致命后果：非终态显式映射回退到世界转换表中的终态转换。
    for transition in transitions_from(contract, current_state):
        declared_severity = str(
            transition.get("severity") or ""
        ).strip().lower()
        if declared_severity and declared_severity != severity:
            continue
        if (
            not declared_severity
            and str(transition.get("cause") or "").strip().lower()
            in {"rescue_success", "recovery"}
        ):
            continue
        target = states.get(str(transition.get("to") or ""))
        if target is None:
            continue
        if severity == "serious" and not bool(target.get("terminal")):
            return str(transition["to"]), target, transition
        if severity == "lethal" and rescue_window:
            if bool(transition.get("opens_rescue_window")) and not bool(
                target.get("terminal")
            ):
                return str(transition["to"]), target, transition
        elif severity == "lethal" and bool(target.get("terminal")):
            return str(transition["to"]), target, transition
    raise InvalidConsequence(
        "世界未声明本次后果对应的状态转换"
        if severity == "serious"
        else "世界未声明致命后果的终态或救援转换"
    )


def _window_rescue_from(window: Mapping[str, Any]) -> str:
    """救援窗口覆盖的命运起点：success/failure 转换的 from 状态。"""

    for key in ("success_transition", "failure_transition"):
        value = window.get(key)
        if isinstance(value, Mapping):
            from_state = str(value.get("from") or "").strip()
        elif (
            isinstance(value, Sequence)
            and not isinstance(value, (str, bytes))
            and len(value) == 2
        ):
            from_state = str(value[0] or "").strip()
        else:
            from_state = ""
        if from_state:
            return from_state
    return ""


def _noop_consequence_record(
    *,
    actor_ref: str,
    from_state: str,
    consequence: Mapping[str, Any],
    sequence: int,
    created_at: str,
    event_ref: str,
) -> dict[str, Any]:
    """跳过/幂等复用时的无变化记录（不调用 apply_transition）。"""

    payload = _mapping(consequence)
    return {
        "actor_ref": str(actor_ref or ""),
        "from_state": from_state,
        "to_state": from_state,
        "reason": str(payload.get("reason") or ""),
        "source": str(payload.get("source") or ""),
        "reversible": False,
        "opens_rescue_window": False,
        "rescue_window_kind": "",
        "consumed_protection_resource": "",
        "sequence": int(sequence or 0),
        "created_at": str(created_at or ""),
        "event_ref": str(event_ref or ""),
    }


def _protection_downgrade(
    contract: Mapping[str, Any],
    current_state: str,
    protection: Mapping[str, int] | None,
) -> tuple[str, str, dict[str, Any] | None]:
    """致命后果的保护资源降级：找到消耗保护资源且目标非终态的转换。"""

    if not protection:
        return "", "", None
    for transition in transitions_from(contract, current_state):
        resource = str(transition.get("consumes_protection_resource") or "")
        if not resource:
            continue
        target = state_definition(contract, str(transition.get("to") or ""))
        if target is None or bool(target.get("terminal")):
            continue
        if int(protection.get(resource, 0) or 0) > 0:
            return str(transition["to"]), resource, transition
    return "", "", None


def _rescue_downgrade(
    contract: Mapping[str, Any],
    current_state: str,
    protection: Mapping[str, int] | None,
) -> tuple[str, dict[str, Any] | None]:
    """Return a declared non-terminal rescue transition, if one is usable.

    A lethal consequence may not skip a world-declared rescue route merely
    because a host/model omitted ``rescue_window``.  Prefer lethal transitions
    over serious fallbacks, while respecting declared protection costs.
    """

    candidates: list[dict[str, Any]] = []
    for transition in transitions_from(contract, current_state):
        target = state_definition(contract, str(transition.get("to") or ""))
        if (
            target is None
            or bool(target.get("terminal"))
            or not bool(transition.get("opens_rescue_window"))
        ):
            continue
        resource = str(
            transition.get("consumes_protection_resource") or ""
        )
        if resource and int((protection or {}).get(resource, 0) or 0) <= 0:
            continue
        candidates.append(dict(transition))
    candidates.sort(
        key=lambda item: (
            0 if str(item.get("severity") or "") == "lethal" else 1,
            str(item.get("to") or ""),
        )
    )
    if not candidates:
        return "", None
    selected = candidates[0]
    return str(selected.get("to") or ""), selected


def resolve_structured_consequence(
    *,
    contract: Mapping[str, Any],
    actor_ref: str,
    current_state: str,
    consequence: Mapping[str, Any],
    sequence: int = 0,
    created_at: str = "",
    event_ref: str = "",
    protection: Mapping[str, int] | None = None,
    open_window: Mapping[str, Any] | None = None,
    allow_direct_terminal: bool = False,
) -> dict[str, Any]:
    """结算 AI 提交的结构化后果（D1_PLAN 18 §4）。

    AI 只能提交 severity/source/reason 等事实，宿主根据当前状态、
    保护资源与世界转换表计算目标状态。返回记录、保护资源快照与
    是否开启救援窗口，供宿主在同一事务内持久化。

    幂等与跳过语义（D1_PLAN 18 §5、§16）：
    - 角色已处于救援窗口覆盖的状态且窗口仍开启时，重复的普通或
      致命后果不再改变命运，
      返回 ``skipped=True`` 且 ``reused_open_window=True`` 的结果；
    - 普通后果（serious）没有合法非终态转换时返回 ``skipped=True``
      的明确结果，普通失败不会直接导致角色死亡；
    - 只有结构非法输入（缺字段、致命后果未展示替代方案等）才抛
      ``InvalidConsequence`` 使整回合回滚。
    """

    payload = _mapping(consequence)
    severity = str(payload.get("severity") or "").strip().lower()
    if severity not in {"serious", "lethal"}:
        raise InvalidConsequence(
            "后果等级必须为 serious 或 lethal"
        )
    if not str(payload.get("target_actor") or "").strip():
        raise InvalidConsequence("后果必须指定目标角色")
    if not str(payload.get("source") or "").strip():
        raise InvalidConsequence("后果必须说明来源")
    if not str(payload.get("reason") or "").strip():
        raise InvalidConsequence("后果必须说明原因")
    alternatives_shown = bool(payload.get("alternatives_shown"))
    if severity == "lethal" and not alternatives_shown:
        raise InvalidConsequence("致命后果必须先向玩家展示替代方案")
    if is_terminal(contract, current_state):
        # 终态不可作为转换起点，也不能再接收命运后果（D1_PLAN 18 §3.3）。
        raise InvalidConsequence("终态角色不能再接收命运后果")

    open_window_data = _mapping(open_window)
    if (
        str(open_window_data.get("status") or "") == "open"
        and _window_rescue_from(open_window_data) == current_state
        and severity in {"serious", "lethal"}
    ):
        # 窗口幂等：角色正处于救援窗口覆盖的命运状态，重复后果并入
        # 现有窗口，不再结算新转换、不开新窗口（D1_PLAN 18 §5）。
        return {
            "skipped": True,
            "reused_open_window": True,
            "message": (
                "角色正处于需要救援的命运状态，救援窗口仍在进行中；"
                "本次后果并入现有窗口，命运状态保持不变。"
            ),
            "record": _noop_consequence_record(
                actor_ref=actor_ref,
                from_state=current_state,
                consequence=payload,
                sequence=sequence,
                created_at=created_at,
                event_ref=event_ref,
            ),
            "protection": dict(protection or {}),
            "target_state": current_state,
            "effective_severity": severity,
            "opens_rescue_window": False,
            "rescue_window_kind": "",
            "window": dict(open_window_data),
        }

    target_state = ""
    opened_window = False
    window_kind = ""
    consumed_resource = ""
    protection_after = dict(protection or {})
    effective_severity = severity
    transition: dict[str, Any] | None = None
    if severity == "lethal":
        downgrade_target, resource, downgrade_transition = _protection_downgrade(
            contract, current_state, protection
        )
        if downgrade_target:
            target_state = downgrade_target
            consumed_resource = resource
            transition = downgrade_transition
            protection_after = dict(protection or {})
            protection_after[resource] = max(
                int(protection_after.get(resource, 0)) - 1, 0
            )
            effective_severity = "serious"
        else:
            target_state, rescue_transition = _rescue_downgrade(
                contract,
                current_state,
                protection,
            )
            if rescue_transition is not None:
                transition = rescue_transition
            elif allow_direct_terminal:
                target_state, _, lethal_transition = _consequence_target(
                    contract, current_state, severity, False
                )
                transition = lethal_transition
            else:
                raise InvalidConsequence(
                    "致命结果不能由主持人或模型直接提交。"
                    "系统没有修改角色命运；请先使用世界声明的救援窗口，"
                    "或取得角色本人确认/世界明确授权后重试。"
                )
    else:
        try:
            target_state, _, serious_transition = _consequence_target(
                contract, current_state, severity, False
            )
        except InvalidConsequence as exc:
            # 普通失败不直接死亡：世界未声明合法非终态转换时跳过本次
            # 后果并返回明确提示，不抛错回滚整回合（D1_PLAN 18 §16）。
            return {
                "skipped": True,
                "reused_open_window": False,
                "message": (
                    "未找到适用于本次普通后果的合法命运转换，后果已跳过；"
                    "普通失败不会直接导致角色死亡，命运状态保持不变。"
                ),
                "reason": str(exc),
                "record": _noop_consequence_record(
                    actor_ref=actor_ref,
                    from_state=current_state,
                    consequence=payload,
                    sequence=sequence,
                    created_at=created_at,
                    event_ref=event_ref,
                ),
                "protection": dict(protection or {}),
                "target_state": current_state,
                "effective_severity": severity,
                "opens_rescue_window": False,
                "rescue_window_kind": "",
            }
        transition = serious_transition

    # Any selected transition may open a rescue window, including a serious
    # fallback and a protection-resource downgrade.  Derive this from the
    # trusted transition rather than from the model's rescue_window boolean.
    if transition is not None and bool(transition.get("opens_rescue_window")):
        opened_window = True
        window_kind = str(
            transition.get("rescue_window_kind") or "default"
        )

    record = apply_transition(
        contract=contract,
        actor_ref=actor_ref,
        from_state=current_state,
        to_state=target_state,
        transition=transition,
        reason=str(payload.get("reason") or ""),
        source=str(payload.get("source") or ""),
        sequence=sequence,
        created_at=created_at,
        event_ref=event_ref,
        protection=protection,
    )
    return {
        "record": record.to_dict(),
        "protection": protection_after,
        "target_state": target_state,
        "effective_severity": effective_severity,
        "opens_rescue_window": opened_window,
        "rescue_window_kind": window_kind,
    }


def _rescue_window_definition(
    contract: Mapping[str, Any],
    kind: str,
) -> dict[str, Any]:
    kind = str(kind or "").strip() or "default"
    for item in _sequence(contract.get("rescue_windows")):
        if str(item.get("kind") or "") == kind:
            return dict(item)
    raise InvalidConsequence(f"世界未声明救援窗口 {kind}")


def open_rescue_window(
    *,
    contract: Mapping[str, Any],
    actor_ref: str,
    kind: str = "default",
    opened_at: str = "",
    expires_on: str = "",
    windows: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """创建救援窗口（幂等：同一角色同一窗口已开启时不重复创建）。"""

    current = [dict(item) for item in (windows or [])]
    actor_ref = str(actor_ref or "").strip()
    kind = str(kind or "").strip() or "default"
    for window in current:
        if (
            str(window.get("actor_ref") or "") == actor_ref
            and str(window.get("kind") or "") == kind
            and str(window.get("status") or "") == "open"
        ):
            return {
                "status": "already_open",
                "window": dict(window),
                "windows": current,
            }
    definition = _rescue_window_definition(contract, kind)
    window = RescueWindow(
        actor_ref=actor_ref,
        kind=kind,
        status="open",
        opened_at=str(opened_at or ""),
        expires_on=str(expires_on or ""),
        allowed_rescue_commands=tuple(
            str(item)
            for item in _sequence(definition.get("allowed_rescue_commands"))
        ),
        success_transition=tuple(
            str(item)
            for item in _sequence(definition.get("success_transition"))
        ),
        failure_transition=tuple(
            str(item)
            for item in _sequence(definition.get("failure_transition"))
        ),
        command_labels={
            str(key): str(value)
            for key, value in _mapping(definition.get("command_labels")).items()
        },
    ).to_dict()
    return {
        "status": "created",
        "window": window,
        "windows": current + [window],
    }


def complete_rescue_window(
    *,
    contract: Mapping[str, Any],
    window: Mapping[str, Any],
    command: str,
    completed_at: str = "",
    windows: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """完成救援窗口（幂等：已完成的窗口不会再次结算转换）。"""

    current = [dict(item) for item in (windows or [])]
    existing = _mapping(window)
    if str(existing.get("status") or "") != "open":
        return {
            "status": "already_completed",
            "outcome": str(existing.get("status") or ""),
            "window": dict(existing),
            "windows": current,
            "transition": None,
        }
    command = str(command or "").strip()
    allowed = {
        str(item)
        for item in _sequence(existing.get("allowed_rescue_commands"))
    }
    if command not in allowed:
        return {
            "status": "command_not_allowed",
            "outcome": "",
            "window": dict(existing),
            "windows": current,
            "transition": None,
        }
    kind = str(existing.get("kind") or "default")
    definition = _rescue_window_definition(contract, kind)
    success_commands = {
        str(item)
        for item in _sequence(definition.get("success_commands"))
    }
    failure_commands = {
        str(item)
        for item in _sequence(definition.get("failure_commands"))
    }
    if command in failure_commands:
        outcome = "failed"
        transition = tuple(
            str(item)
            for item in _sequence(existing.get("failure_transition"))
        )
    elif command in success_commands:
        outcome = "succeeded"
        transition = tuple(
            str(item)
            for item in _sequence(existing.get("success_transition"))
        )
    else:
        return {
            "status": "command_not_allowed",
            "outcome": "",
            "window": dict(existing),
            "windows": current,
            "transition": None,
        }
    updated = dict(existing)
    updated.update(
        {
            "status": outcome,
            "outcome": outcome,
            "command": command,
            "completed_at": str(completed_at or ""),
        }
    )
    replaced: list[dict[str, Any]] = []
    for item in current:
        if (
            str(item.get("actor_ref")) == str(existing.get("actor_ref"))
            and str(item.get("kind")) == kind
            and str(item.get("status")) == "open"
        ):
            replaced.append(updated)
        else:
            replaced.append(item)
    if not any(
        str(item.get("actor_ref")) == str(existing.get("actor_ref"))
        and str(item.get("kind")) == kind
        and str(item.get("status")) == outcome
        for item in replaced
    ):
        replaced.append(updated)
    return {
        "status": outcome,
        "outcome": outcome,
        "window": updated,
        "windows": replaced,
        "transition": {"from": transition[0], "to": transition[1]},
    }


def expire_rescue_windows(
    *,
    contract: Mapping[str, Any],
    windows: Sequence[Mapping[str, Any]],
    now: str = "",
    strict: bool = False,
) -> dict[str, Any]:
    """到期结算救援窗口（幂等：重复调用不会重复结算）。"""

    current = [dict(item) for item in (windows or [])]
    now = str(now or "")
    expired: list[dict[str, Any]] = []
    remaining: list[dict[str, Any]] = []
    for window in current:
        if str(window.get("status") or "") != "open":
            remaining.append(window)
            continue
        expires_on = str(window.get("expires_on") or "")
        if not expires_on or not now:
            remaining.append(window)
            continue
        overdue = now > expires_on if strict else now >= expires_on
        if not overdue:
            remaining.append(window)
            continue
        kind = str(window.get("kind") or "default")
        definition = _rescue_window_definition(contract, kind)
        updated = dict(window)
        updated.update(
            {
                "status": "failed",
                "outcome": "expired",
                "completed_at": now,
                "command": "",
            }
        )
        expired.append(updated)
        remaining.append(updated)
    return {
        "expired": expired,
        "windows": remaining,
    }


def _membership_filter(
    contract: Mapping[str, Any],
    override: Mapping[str, Any] | None,
) -> dict[str, Any]:
    merged = dict(DEFAULT_MEMBERSHIP_FILTER)
    declared = _mapping(contract.get("membership_filter"))
    merged.update(declared)
    merged.update(_mapping(override))
    return merged


def project_party_summary(
    *,
    contract: Mapping[str, Any],
    members: Sequence[Mapping[str, Any]],
    membership_filter: Mapping[str, Any] | None = None,
) -> PartySummary:
    """队伍聚合：只统计合法成员，排除旁观/未确认/离队/NPC/召唤等。"""

    merged = _membership_filter(contract, membership_filter)
    role_types = {
        str(item)
        for item in _sequence(merged.get("role_types"))
    } or {"player"}
    participation_statuses = {
        str(item)
        for item in _sequence(merged.get("participation_statuses"))
    } or {"active"}
    card_statuses = {
        str(item)
        for item in _sequence(merged.get("card_statuses"))
    }
    card_stages = {
        str(item)
        for item in _sequence(merged.get("card_stages"))
    }
    exclude_kinds = {
        str(item)
        for item in _sequence(merged.get("exclude_entity_kinds"))
    }
    require_confirmed = bool(merged.get("require_confirmed", True))

    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for raw in members:
        member = dict(raw)
        member_ref = str(member.get("actor_ref") or member.get("id") or "")
        role_type = str(member.get("role_type") or "player").lower()
        participation = str(member.get("participation_status") or "active").lower()
        card_status = str(member.get("card_status") or "approved").lower()
        card_stage = str(member.get("card_stage") or "").lower()
        entity_kind = str(member.get("entity_kind") or "").lower()
        reasons: list[str] = []
        if entity_kind in exclude_kinds:
            reasons.append("非玩家实体")
        if role_type not in role_types:
            reasons.append("角色类型不计入队伍")
        if participation not in participation_statuses:
            reasons.append("参与状态不计入队伍")
        # 建卡阶段与确认状态是两个独立门槛：任一不满足即排除，
        # 已分阶段但未确认的角色同样不得计入队伍。
        if card_stage and card_stage not in card_stages:
            reasons.append("建卡阶段未达到出场资格")
        if require_confirmed and card_status not in card_statuses:
            reasons.append("角色卡未确认")
        fate_state = str(member.get("state") or "").strip()
        state = state_definition(contract, fate_state)
        if state is None and fate_state:
            reasons.append("命运状态未在世界注册")
        if reasons:
            excluded.append(
                {
                    "actor_ref": member_ref,
                    "reasons": reasons,
                }
            )
            continue
        terminal = bool(state and state.get("terminal"))
        active = bool(state and state.get("can_act", not terminal))
        included.append(
            {
                "actor_ref": member_ref,
                "state": fate_state,
                "terminal": terminal,
                "can_act": active,
                "role_type": role_type,
                "participation_status": participation,
            }
        )
    member_count = len(included)
    dead_count = sum(1 for member in included if member["terminal"])
    incapacitated_count = sum(
        1
        for member in included
        if not member["terminal"] and not member["can_act"]
    )
    living_count = member_count - dead_count
    return PartySummary(
        member_count=member_count,
        living_count=living_count,
        dead_count=dead_count,
        incapacitated_count=incapacitated_count,
        members=tuple(included),
        excluded=tuple(excluded),
    )


def project_fate(
    *,
    contract: Mapping[str, Any],
    state_id: str,
    rescue_window: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """玩家可见命运投影（D1_PLAN 18 §13.1）：不暴露内部状态 id。"""

    state = state_definition(contract, state_id)
    if state is None:
        return {
            "label": "未知状态",
            "can_act": False,
            "terminal": False,
            "message": "命运状态尚未初始化。",
            "available_actions": [],
        }
    terminal = bool(state.get("terminal"))
    active = bool(state.get("can_act", not terminal))
    declared_message = str(state.get("message") or "").strip()
    if declared_message:
        message = declared_message
    elif terminal:
        message = "角色已进入终态，可保留遗言、遗物处理与复盘输入。"
    elif not active:
        message = "需要在下一次场景推进前完成救援。"
    else:
        message = ""
    available_actions: list[str] = []
    window = _mapping(rescue_window)
    if not terminal and str(window.get("status") or "") == "open":
        labels = _mapping(window.get("command_labels"))
        for command in _sequence(window.get("allowed_rescue_commands")):
            label = str(labels.get(command) or "")
            if label:
                available_actions.append(label)
    return {
        "label": str(state.get("label") or state_id),
        "can_act": active,
        "terminal": terminal,
        "message": message,
        "available_actions": available_actions,
    }


def party_terminal_references(condition: Mapping[str, Any]) -> bool:
    """终局条件是否引用队伍生死投影（用于空队伍保护兜底）。"""

    from ..contracts.actor_fate import collect_condition_paths

    return bool(
        collect_condition_paths(condition.get("when"))
        & PARTY_PROTECTED_PATHS
    )


def empty_party_blocked(
    contract: Mapping[str, Any],
    condition: Mapping[str, Any],
    context: Mapping[str, Any],
) -> bool:
    """空队伍保护兜底：队伍为空且条件引用生死计数时强制不触发。"""

    party = _mapping(context.get("party"))
    if int(party.get("member_count", 0) or 0) > 0:
        return False
    if not party_terminal_references(condition):
        return False
    from ..contracts.actor_fate import condition_has_member_guard

    return not condition_has_member_guard(condition.get("when"))


__all__ = [
    "DEFAULT_MEMBERSHIP_FILTER",
    "InsufficientProtection",
    "InvalidConsequence",
    "InvalidFateTransition",
    "apply_transition",
    "can_act",
    "complete_rescue_window",
    "consume_protection",
    "empty_party_blocked",
    "expire_rescue_windows",
    "find_transition",
    "is_terminal",
    "open_rescue_window",
    "party_terminal_references",
    "project_fate",
    "project_party_summary",
    "resolve_structured_consequence",
    "state_definition",
    "transitions_from",
]
