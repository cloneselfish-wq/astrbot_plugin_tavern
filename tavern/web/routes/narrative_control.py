"""D1-ARC-003：叙述控制路由（NarrativeControlView / DM 命令验证）。

- ``narrative_control_view``：只输出中文语义的 NarrativeControlView，
  普通视图不暴露 ``active_dm_user_id`` / revision；归档副本标记
  ``readonly`` 并抑制管理动作。
- ``validate_dm_command``：纯校验，不执行、不写库、不投递；返回规范化
  ``plan`` 供宿主层执行（事务与 outbox 由仓储层负责）。
"""

from __future__ import annotations

import json as _json
from collections.abc import Mapping
from typing import Any

from ...chat_experience import normalize_chat_experience
from ...contracts.web_views import project_narrative_control_view
from ..errors import bad_request, forbidden, not_found

from .sessions import require_member

#: DM 指令 → 世界包 chat_experience.dm 策略键。
DM_POLICY_KEYS: dict[str, str] = {
    "directive": "allow_narrative_override",
    "narrative": "allow_narrative_override",
    "whisper": "allow_secret_whispers",
    "manual_roll": "allow_manual_checks",
    "adjust_relationship": "allow_state_intervention",
    "adjust_economy": "allow_state_intervention",
    "set_next_actor": "allow_state_intervention",
    "lock_action": "allow_state_intervention",
    "lock_input": "allow_state_intervention",
    "replace_choices": "allow_state_intervention",
    "force_end_vote": "allow_state_intervention",
    "vote_as": "allow_state_intervention",
}

SUPPORTED_COMMANDS = frozenset(
    {
        "enable_dm",
        "disable_dm",
        "directive",
        "narrative",
        "announce",
        "whisper",
        "set_next_actor",
        "lock_action",
        "lock_input",
        "replace_choices",
        "force_end_vote",
        "vote_as",
        "manual_roll",
        "adjust_relationship",
        "adjust_economy",
        "pause",
        "resume",
        "checkpoint",
        "cancel_operation",
    }
)


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _bool(value: Any) -> bool:
    return bool(value)


async def resolve_participant_ref(
    database: Any,
    session_id: str,
    reference: str,
) -> dict[str, Any] | None:
    """按参与者 ID / 群用户 ID / 角色名解析目标角色（DM 密语与操作）。"""
    reference = _text(reference)
    if not reference:
        return None
    try:
        roster = await database.list_roster(session_id)
    except Exception:
        return None
    for item in roster or ():
        if not isinstance(item, Mapping):
            continue
        if _text(item.get("id")) == reference:
            return dict(item)
        if _text(item.get("group_user_id")) == reference:
            return dict(item)
    lowered = reference.lower()
    for item in roster or ():
        if not isinstance(item, Mapping):
            continue
        for key in ("character_name", "display_name"):
            if _text(item.get(key)).lower() == lowered:
                return dict(item)
    return None


async def narrative_control_view(
    database: Any,
    session_id: str,
    principal: Mapping[str, Any],
) -> dict[str, Any]:
    """NarrativeControlView 纯投影（中文语义，归档只读）。"""
    session_id = _text(session_id)
    if not session_id:
        raise bad_request(
            "缺少 session_id",
            recovery="请选择一个要查看的副本。",
        )
    role = await require_member(database, session_id, principal)
    session = await database.get_session(session_id)
    session = dict(session) if isinstance(session, Mapping) else {}
    control = await database.get_control_state(session_id)
    try:
        archive = await database.get_session_archive(session_id)
    except Exception:
        archive = None
    readonly = bool(
        isinstance(archive, Mapping) and archive.get("readonly")
    ) or _text(session.get("state")) == "finished"
    try:
        roster = await database.list_roster(session_id)
    except Exception:
        roster = []
    host_labels = {
        _text(item.get("group_user_id") or item.get("user_id")): _text(
            item.get("display_name") or item.get("character_name")
        )
        for item in roster
        if isinstance(item, Mapping)
        and _text(item.get("group_user_id") or item.get("user_id"))
    }
    try:
        pending = await database.pending_operations(session_id)
    except Exception:
        pending = []
    dm_user_id = _text(control.get("active_dm_user_id"))
    can_manage = role in {"dm", "admin"} and not readonly
    view = project_narrative_control_view(
        control,
        host_label=host_labels.get(dm_user_id, ""),
        input_locked=_bool(session.get("input_locked")),
        pending_actions=[
            {
                "id": _text(item.get("operation_id") or item.get("id")),
                "label": _text(
                    item.get("label") or item.get("status") or "待处理操作"
                ),
            }
            for item in pending
            if isinstance(item, Mapping)
        ],
        permissions={
            "can_manage": can_manage,
            "can_view_private": role in {"dm", "admin"},
        },
        include_technical_refs=bool(principal.get("is_admin")),
    )
    view["viewer_role"] = role
    view["readonly"] = readonly
    view["can_manage"] = can_manage
    return view


async def _validate_command_params(
    database: Any,
    session_id: str,
    command: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """按指令做参数规范化与必填校验，返回宿主层可执行的最小 plan。"""
    if command in {"enable_dm", "disable_dm", "pause", "resume", "checkpoint"}:
        return {"command": command}
    if command == "directive":
        directive = _text(payload.get("directive"))
        if not directive:
            raise bad_request(
                "主持指令不能为空",
                recovery="请输入要设置的主持指令内容。",
            )
        return {"command": command, "directive": directive}
    if command == "narrative":
        narrative = _text(
            payload.get("narrative") or payload.get("text")
        )
        if not narrative:
            raise bad_request(
                "追加叙事不能为空",
                recovery="请输入要追加的叙事内容。",
            )
        return {
            "command": command,
            "narrative": narrative,
            "mode": _text(payload.get("mode"), "append"),
        }
    if command == "announce":
        text = _text(payload.get("text"))
        if not text:
            raise bad_request(
                "公告内容不能为空",
                recovery="请输入要发布的公告内容。",
            )
        return {"command": command, "text": text}
    if command == "whisper":
        whisper_ref = _text(
            payload.get("participant_id") or payload.get("target")
        )
        if not whisper_ref:
            raise bad_request(
                "缺少密语目标角色",
                recovery="请选择当前页中的角色后再发送密语。",
            )
        text = _text(payload.get("text"))
        if not text:
            raise bad_request(
                "密语内容不能为空",
                recovery="请输入要发送的密语内容。",
            )
        participant = await resolve_participant_ref(
            database, session_id, whisper_ref
        )
        if participant is None:
            raise not_found(
                "未找到密语目标角色",
                recovery="请使用当前页角色序号或完整角色名重试。",
            )
        return {
            "command": command,
            "participant": {
                "id": _text(participant.get("id")),
                "character_name": _text(
                    participant.get("character_name")
                    or participant.get("display_name")
                ),
                "display_name": _text(participant.get("display_name")),
            },
            "text": text,
        }
    if command == "set_next_actor":
        user_id = _text(payload.get("user_id"))
        if not user_id:
            raise bad_request(
                "缺少下一位行动者",
                recovery="请选择要指定为下一位行动者的成员。",
            )
        return {"command": command, "user_id": user_id}
    if command == "lock_action":
        participant_id = _text(payload.get("participant_id"))
        if not participant_id:
            raise bad_request(
                "缺少要锁定的角色",
                recovery="请选择要锁定行动的角色。",
            )
        return {
            "command": command,
            "participant_id": participant_id,
            "locked": _bool(payload.get("locked", True)),
        }
    if command == "lock_input":
        return {
            "command": command,
            "locked": _bool(payload.get("locked", True)),
        }
    if command == "replace_choices":
        raw = payload.get("choices")
        if isinstance(raw, list):
            parsed = raw
        else:
            try:
                parsed = _json.loads(
                    _text(payload.get("choices_json"), "[]")
                )
            except _json.JSONDecodeError:
                parsed = []
        choices = [dict(item) for item in parsed if isinstance(item, Mapping)]
        if not choices:
            raise bad_request(
                "替换候选项不能为空",
                recovery="请提供至少一个候选项。",
            )
        return {"command": command, "choices": choices}
    if command == "force_end_vote":
        return {
            "command": command,
            "winner_key": _text(payload.get("winner_key")),
        }
    if command == "vote_as":
        user_id = _text(payload.get("user_id"))
        key = _text(payload.get("key"))
        if not user_id or not key:
            raise bad_request(
                "缺少投票成员或候选项",
                recovery="请选择投票成员与候选项序号。",
            )
        return {"command": command, "user_id": user_id, "key": key}
    if command == "manual_roll":
        participant_id = _text(payload.get("participant_id"))
        stat = _text(payload.get("stat"))
        if not participant_id or not stat:
            raise bad_request(
                "缺少检定角色或属性",
                recovery="请选择检定角色与属性名称。",
            )
        try:
            total = int(payload.get("total") or 0)
        except (TypeError, ValueError, OverflowError):
            raise bad_request(
                "检定结果必须是整数",
                recovery="请重新输入整数检定结果。",
            )
        return {
            "command": command,
            "participant_id": participant_id,
            "stat": stat,
            "total": total,
            "note": _text(payload.get("note")),
        }
    if command == "adjust_relationship":
        source = _text(payload.get("source"))
        target = _text(payload.get("target"))
        if not source or not target:
            raise bad_request(
                "缺少关系双方",
                recovery="请选择要调整关系的两个角色。",
            )
        try:
            delta = int(payload.get("delta") or 0)
        except (TypeError, ValueError, OverflowError):
            raise bad_request(
                "关系调整值必须是整数",
                recovery="请重新输入整数调整值。",
            )
        return {
            "command": command,
            "source": source,
            "target": target,
            "dimension": _text(payload.get("dimension"), "信任"),
            "delta": delta,
        }
    if command == "adjust_economy":
        currency_id = _text(payload.get("currency_id"))
        amount = payload.get("amount")
        if not currency_id or amount is None:
            raise bad_request(
                "缺少货币或调整数额",
                recovery="请选择货币并输入调整数额。",
            )
        if not isinstance(amount, (int, float)):
            raise bad_request(
                "调整数额必须是数字",
                recovery="请重新输入数字数额。",
            )
        return {
            "command": command,
            "currency_id": currency_id,
            "amount": amount,
            "kind": _text(payload.get("kind"), "adjust"),
            "reason": _text(payload.get("reason")),
            "operation_id": _text(payload.get("operation_id")),
        }
    if command == "cancel_operation":
        operation_id = _text(payload.get("operation_id"))
        if not operation_id:
            raise bad_request(
                "缺少待取消的操作",
                recovery="请选择要取消的卡住操作。",
            )
        return {
            "command": command,
            "operation_id": operation_id,
            "reason": _text(payload.get("reason")),
        }
    return {"command": command}


async def validate_dm_command(
    payload: Mapping[str, Any],
    database: Any,
    principal: Mapping[str, Any],
) -> dict[str, Any]:
    """校验 DM 指令（权限 / 只读 / 世界策略 / 参数），不执行任何写入。"""
    payload = dict(payload) if isinstance(payload, Mapping) else {}
    session_id = _text(payload.get("session_id"))
    command = _text(payload.get("command"))
    if not session_id:
        raise bad_request(
            "缺少 session_id",
            recovery="请先选择要操作的副本。",
        )
    if not command:
        raise bad_request(
            "缺少 command",
            recovery="请指定要执行的主持指令。",
        )
    role = await require_member(database, session_id, principal)
    if role not in {"dm", "admin"}:
        raise forbidden(
            "需要副本 DM 或管理员权限，无法执行主持指令",
            recovery="请由副本主持人在主持面板中操作。",
        )
    try:
        archive = await database.get_session_archive(session_id)
    except Exception:
        archive = None
    if isinstance(archive, Mapping) and archive.get("readonly"):
        readonly = True
    else:
        session = await database.get_session(session_id)
        readonly = (
            _text(session.get("state")) == "finished"
            if isinstance(session, Mapping)
            else False
        )
    if readonly:
        raise forbidden(
            "副本已归档为只读，无法执行主持指令",
            code="session_readonly",
            recovery="归档副本仅可查看；如需继续冒险请由管理员另开新副本。",
        )
    if command not in SUPPORTED_COMMANDS:
        raise bad_request(
            f"不支持的主持指令：{command}",
            recovery=(
                "支持：directive / narrative / announce / whisper / "
                "manual_roll / adjust_relationship / adjust_economy / "
                "pause / resume / checkpoint / cancel_operation 等。"
            ),
        )
    required_policy = DM_POLICY_KEYS.get(command)
    if required_policy:
        instance = await database.get_instance_config(session_id)
        dm_policy = normalize_chat_experience(
            instance.get("world_snapshot")
            if isinstance(instance, Mapping)
            else {}
        )["dm"]
        if not _bool(dm_policy.get(required_policy, True)):
            raise forbidden(
                f"当前世界包已关闭该人工 DM 能力（{required_policy}）",
                recovery="请联系管理员在世界包配置中开启对应 DM 能力。",
            )
    plan = await _validate_command_params(
        database, session_id, command, payload
    )
    return {
        "ok": True,
        "command": command,
        "session_id": session_id,
        "plan": plan,
    }


__all__ = [
    "DM_POLICY_KEYS",
    "SUPPORTED_COMMANDS",
    "narrative_control_view",
    "resolve_participant_ref",
    "validate_dm_command",
]
