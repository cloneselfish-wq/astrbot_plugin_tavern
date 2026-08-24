"""Validated RC10 cross-module gameplay state and tactical/challenge resolver."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from typing import Any

GAMEPLAY_RUNTIME_MODULES = frozenset(
    {
        "elemental_interactions",
        "knowledge_graph",
        "evidence_ledger",
        "accords",
        "assembly",
        "rumor_network",
        "scene_environment",
        "challenge_engine",
        "tactical_conflict",
        "quest_graph",
        "time_clock",
        "relationship_graph",
        "npc_lifecycle",
        "actor_fate",
        "items_inventory",
        "economy",
        "ending",
    }
)
VISIBILITIES = frozenset({"public", "party", "dm", "admin"})
ACCORD_STATES = frozenset(
    {"draft", "proposed", "accepted", "active", "fulfilled", "breached", "expired", "voided", "disputed"}
)
CHALLENGE_OUTCOMES = frozenset(
    {"active", "success", "partial", "failure_forward", "retreat", "negotiated", "aborted"}
)
TACTICAL_OUTCOMES = frozenset(
    {"active", "victory", "partial_success", "retreat", "negotiated", "defeat_forward", "aborted_by_host"}
)
SEMANTIC_EVENT_KINDS = frozenset(
    {
        "challenge_started", "challenge_action_submitted", "challenge_phase_changed",
        "challenge_ended", "conflict_started", "intent_submitted", "intent_replaced",
        "round_locked", "roll_locked", "effect_applied", "reaction_triggered",
        "environment_advanced", "fate_changed", "objective_changed", "phase_changed",
        "conflict_ended", "correction_applied", "runtime_archived",
    }
)
ASSEMBLY_STATES = frozenset(
    {"scheduled", "credentialing", "agenda_open", "hearing", "deliberation", "voting", "certified", "recessed", "failed", "contested"}
)


def validate_state_payload(module_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    module = str(module_id or "").strip()
    if module not in GAMEPLAY_RUNTIME_MODULES:
        raise ValueError("当前世界未声明该 RC10 玩法模块")
    item = dict(payload)
    if len(json.dumps(item, ensure_ascii=False)) > 64_000:
        raise ValueError("玩法状态超过单项安全上限")
    visibility = str(item.get("visibility") or "public")
    if visibility not in VISIBILITIES:
        raise ValueError("玩法状态可见范围无效")
    if module == "accords" and str(item.get("status") or "draft") not in ACCORD_STATES:
        raise ValueError("承诺状态转换无效")
    if module == "assembly" and str(item.get("status") or "scheduled") not in ASSEMBLY_STATES:
        raise ValueError("会盟阶段无效")
    if module == "evidence_ledger":
        for key in ("integrity", "reliability"):
            value = float(item.get(key, 0.5))
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{key} 必须在 0..1 范围")
            item[key] = value
    if module == "elemental_interactions":
        layers = int(item.get("exposure_layers") or 0)
        if not 0 <= layers <= 5:
            raise ValueError("元素暴露层数必须在 0..5 范围")
        item["exposure_layers"] = layers
    if module == "challenge_engine":
        mode = str(item.get("mode") or "")
        if mode not in {"investigation", "social", "chase", "rescue", "hazard", "infiltration", "ritual", "choice", "tactical"}:
            raise ValueError("挑战 mode 未注册")
        phase = str(item.get("phase") or "setup")
        if phase not in {"inactive", "setup", "declare", "locked", "resolve", "settle", "ended"}:
            raise ValueError("挑战阶段无效")
        if not str(item.get("objective") or "").strip():
            raise ValueError("挑战必须声明玩家可理解的目标")
        target = int(item.get("target") or 3)
        progress = int(item.get("progress") or 0)
        if target < 1 or progress < 0 or progress > target:
            raise ValueError("挑战进度必须在 0 到目标值之间")
        outcome = str(item.get("status") or "active")
        if outcome not in CHALLENGE_OUTCOMES:
            raise ValueError("挑战结果未注册")
        item["mode"] = mode
        item["phase"] = phase
        item["target"] = target
        item["progress"] = progress
        item["status"] = outcome
    if module == "tactical_conflict":
        phase = str(item.get("phase") or "setup")
        if phase not in {"setup", "declare", "locked", "resolve_players", "resolve_opposition", "environment", "settle_round", "victory", "partial_success", "retreat", "negotiated", "defeat_forward", "aborted_by_host"}:
            raise ValueError("战术冲突阶段无效")
        round_no = int(item.get("round") or 1)
        if round_no < 1:
            raise ValueError("战术冲突回合必须从 1 开始")
        outcome = str(item.get("status") or "active")
        if outcome not in TACTICAL_OUTCOMES:
            raise ValueError("战术冲突结果未注册")
        item["phase"] = phase
        item["round"] = round_no
        item["status"] = outcome
    item["visibility"] = visibility
    return item


def input_sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_effect_updates(origin_module: str, value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    updates: list[dict[str, Any]] = []
    for raw in value[:16]:
        if not isinstance(raw, Mapping):
            raise ValueError("玩法结果 effect 必须是对象列表")
        module_id = str(raw.get("module_id") or "").strip()
        state_key = str(raw.get("state_key") or "").strip()
        if module_id == "actor_fate":
            raise ValueError(
                "角色命运不能通过通用玩法 effect 修改；必须使用专用命运预览、同意与救援服务"
            )
        if module_id not in GAMEPLAY_RUNTIME_MODULES or module_id == origin_module:
            raise ValueError("玩法结果 effect 目标模块无效")
        if not state_key or len(state_key) > 160:
            raise ValueError("玩法结果 effect 状态键无效")
        expected_raw = raw.get("expected_revision")
        if (
            origin_module in {"challenge_engine", "tactical_conflict"}
            and expected_raw not in (None, "")
            and (
                isinstance(expected_raw, bool)
                or not isinstance(expected_raw, int)
            )
        ):
            raise ValueError("挑战或战术执行计划必须冻结整数 revision")
        expected_revision = None if expected_raw in (None, "") else int(expected_raw)
        if (
            origin_module in {"challenge_engine", "tactical_conflict"}
            and expected_revision is None
        ):
            raise ValueError("挑战或战术执行计划必须冻结关联状态 revision")
        if expected_revision is not None and expected_revision < 0:
            raise ValueError("玩法结果 effect revision 无效")
        operation = str(raw.get("operation") or "replace").strip()
        if operation not in {"replace", "merge"}:
            raise ValueError("玩法结果 effect operation 无效")
        state = raw.get("state")
        if not isinstance(state, Mapping):
            raise ValueError("玩法结果 effect 缺少目标状态")
        updates.append(
            {
                "module_id": module_id,
                "state_key": state_key,
                "expected_revision": expected_revision,
                "operation": operation,
                "state": validate_state_payload(module_id, state),
                "label": str(raw.get("label") or "关联状态已更新").strip()[:120],
            }
        )
    return updates


def validate_item_instance_updates(
    origin_module: str,
    value: Any,
) -> list[dict[str, Any]]:
    """Validate private tactical item CAS plans before repository execution."""

    if value in (None, []):
        return []
    if origin_module != "tactical_conflict":
        raise ValueError("只有战术结算可以提交装备实例更新")
    if not isinstance(value, list):
        raise ValueError("战术装备实例更新必须是对象列表")
    if len(value) > 64:
        raise ValueError("单次战术结算的装备实例更新超过安全上限")

    allowed = {
        "instance_id",
        "owner_type",
        "owner_ref",
        "item_id",
        "quantity_before",
        "quantity_after",
        "durability_before",
        "durability_after",
        "charges_before",
        "charges_after",
    }
    limits = {
        "instance_id": 160,
        "owner_type": 40,
        "owner_ref": 128,
        "item_id": 128,
    }
    owner_types = {"character", "party", "actor"}
    seen_instances: set[str] = set()
    updates: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise ValueError("战术装备实例更新必须是对象列表")
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError("战术装备实例更新包含未注册字段")
        normalized: dict[str, Any] = {}
        for field, limit in limits.items():
            token = str(raw.get(field) or "").strip()
            if not token or len(token) > limit:
                raise ValueError("战术装备实例更新缺少有效实例、所有者或物品")
            normalized[field] = token
        if normalized["owner_type"] not in owner_types:
            raise ValueError("战术装备实例所有者类型无效")
        if normalized["instance_id"] in seen_instances:
            raise ValueError("单次战术结算不能重复更新同一装备实例")
        seen_instances.add(normalized["instance_id"])

        for field in (
            "quantity_before",
            "quantity_after",
            "durability_before",
            "durability_after",
            "charges_before",
            "charges_after",
        ):
            raw_value = raw.get(field)
            if isinstance(raw_value, bool) or not isinstance(raw_value, int):
                raise ValueError("战术装备耐久和充能必须是整数")
            minimum = 1 if field.startswith("quantity_") else 0
            maximum = 1_000_000 if field.startswith("quantity_") else 10000
            if not minimum <= raw_value <= maximum:
                raise ValueError("战术装备数量、耐久或充能超出安全范围")
            normalized[field] = raw_value
        if (
            normalized["quantity_after"] > normalized["quantity_before"]
            or
            normalized["durability_after"] > normalized["durability_before"]
            or normalized["charges_after"] > normalized["charges_before"]
        ):
            raise ValueError("战术装备消耗不能增加数量、耐久或充能")
        if (
            normalized["quantity_after"] == normalized["quantity_before"]
            and
            normalized["durability_after"] == normalized["durability_before"]
            and normalized["charges_after"] == normalized["charges_before"]
        ):
            raise ValueError("战术装备实例更新必须包含实际消耗")
        updates.append(normalized)
    return updates


def validate_character_resource_updates(
    origin_module: str,
    value: Any,
) -> list[dict[str, Any]]:
    if value in (None, []):
        return []
    if origin_module != "tactical_conflict" or not isinstance(value, list):
        raise ValueError("角色资源更新只能由战术结算对象列表生成")
    if len(value) > 64:
        raise ValueError("单次战术结算的角色资源更新超过安全上限")
    allowed = {
        "participant_ref", "actor_ref", "resource_ref",
        "current_before", "current_after", "maximum_before",
    }
    seen: set[tuple[str, str]] = set()
    updates: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, Mapping) or set(raw) - allowed:
            raise ValueError("角色资源更新包含无效结构或未注册字段")
        item = {
            "participant_ref": str(raw.get("participant_ref") or "").strip(),
            "actor_ref": str(raw.get("actor_ref") or "").strip(),
            "resource_ref": str(raw.get("resource_ref") or "").strip(),
        }
        if (
            not item["participant_ref"] or len(item["participant_ref"]) > 160
            or not item["actor_ref"] or len(item["actor_ref"]) > 200
            or not item["resource_ref"] or len(item["resource_ref"]) > 160
        ):
            raise ValueError("角色资源更新缺少有效角色或资源")
        if item["actor_ref"] != f"character:{item['participant_ref']}":
            raise ValueError("角色资源更新的角色绑定不一致")
        if ":" not in item["resource_ref"]:
            raise ValueError("角色资源更新引用无效")
        identity = (item["participant_ref"], item["resource_ref"])
        if identity in seen:
            raise ValueError("单次战术结算不能重复更新同一角色资源")
        seen.add(identity)
        for field in ("current_before", "current_after", "maximum_before"):
            raw_value = raw.get(field)
            if isinstance(raw_value, bool) or not isinstance(raw_value, int):
                raise ValueError("角色资源更新值必须是整数")
            if not 0 <= raw_value <= 1_000_000:
                raise ValueError("角色资源更新值超出安全范围")
            item[field] = raw_value
        if item["current_before"] > item["maximum_before"]:
            raise ValueError("角色资源冻结值超过上限")
        if item["current_after"] > item["maximum_before"]:
            raise ValueError("角色资源结算值超过上限")
        if item["current_after"] == item["current_before"]:
            raise ValueError("角色资源更新必须包含实际变化")
        updates.append(item)
    return updates


def validate_runtime_effect_instance_updates(
    origin_module: str,
    value: Any,
) -> list[dict[str, Any]]:
    if value in (None, []):
        return []
    if origin_module != "tactical_conflict" or not isinstance(value, list):
        raise ValueError("运行时效果实例更新只能由战术结算对象列表生成")
    if len(value) > 32:
        raise ValueError("单次战术结算的运行时效果更新超过安全上限")
    common = {
        "operation", "instance_id", "target_ref", "effect_ref",
        "source_ref", "persistence_scope",
    }
    allowed_scopes = {
        "global_character", "world_character", "campaign",
        "session", "scene", "temporary",
    }
    seen: set[str] = set()
    updates: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise ValueError("运行时效果实例更新必须是对象列表")
        operation = str(raw.get("operation") or "").strip()
        allowed = common | (
            {"state", "duration"}
            if operation == "create"
            else {"status_before", "status_after"}
            if operation == "end"
            else set()
        )
        if operation not in {"create", "end"} or set(raw) - allowed:
            raise ValueError("运行时效果实例更新包含无效操作或未注册字段")
        item = {"operation": operation}
        for field, limit in (
            ("instance_id", 200), ("target_ref", 200),
            ("effect_ref", 200), ("source_ref", 200),
            ("persistence_scope", 40),
        ):
            token = str(raw.get(field) or "").strip()
            if not token or len(token) > limit:
                raise ValueError("运行时效果实例更新缺少有效身份或引用")
            item[field] = token
        if item["persistence_scope"] not in allowed_scopes:
            raise ValueError("运行时效果实例作用域无效")
        if (
            ":" not in item["target_ref"]
            or not item["effect_ref"].startswith("runtime_effect:")
            or ":" not in item["source_ref"]
            or (
                operation == "create"
                and not item["instance_id"].startswith("tactical-effect:")
            )
        ):
            raise ValueError("运行时效果实例引用无效")
        if item["instance_id"] in seen:
            raise ValueError("单次战术结算不能重复更新同一运行时效果实例")
        seen.add(item["instance_id"])
        if operation == "create":
            state = raw.get("state")
            duration = raw.get("duration", {})
            if not isinstance(state, Mapping) or not isinstance(duration, Mapping):
                raise ValueError("创建运行时效果实例缺少有效状态或持续期")
            if len(json.dumps({"state": state, "duration": duration}, ensure_ascii=False)) > 8_000:
                raise ValueError("运行时效果实例状态超过安全上限")
            item["state"] = dict(state)
            item["duration"] = dict(duration)
        else:
            before = str(raw.get("status_before") or "").strip()
            after = str(raw.get("status_after") or "").strip()
            if before != "active" or after != "ended":
                raise ValueError("结束运行时效果实例的状态转换无效")
            item["status_before"] = before
            item["status_after"] = after
        updates.append(item)
    return updates


def validate_semantic_events(module_id: str, value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    events: list[dict[str, Any]] = []
    for raw in value[:32]:
        if not isinstance(raw, Mapping):
            raise ValueError("玩法语义事件必须是对象列表")
        kind = str(raw.get("kind") or "").strip()
        if kind not in SEMANTIC_EVENT_KINDS:
            raise ValueError("玩法语义事件类型未注册")
        visibility = str(raw.get("visibility") or "party").strip()
        if visibility not in VISIBILITIES:
            raise ValueError("玩法语义事件可见范围无效")
        events.append(
            {
                "kind": kind,
                "label": str(raw.get("label") or "玩法状态已更新").strip()[:120],
                "summary": str(raw.get("summary") or "").strip()[:300],
                "visibility": visibility,
                "details": {
                    str(key): value
                    for key, value in dict(raw.get("details") or {}).items()
                    if isinstance(value, (str, int, float, bool))
                },
            }
        )
    return events


def can_view_visibility(viewer_role: str, visibility: str) -> bool:
    role = str(viewer_role or "player")
    if visibility == "public":
        return True
    if visibility == "party":
        return role in {"player", "character", "dm", "admin"}
    if visibility == "dm":
        return role in {"dm", "admin"}
    return role == "admin"


__all__ = [
    "ACCORD_STATES",
    "ASSEMBLY_STATES",
    "CHALLENGE_OUTCOMES",
    "GAMEPLAY_RUNTIME_MODULES",
    "TACTICAL_OUTCOMES",
    "SEMANTIC_EVENT_KINDS",
    "can_view_visibility",
    "input_sha256",
    "validate_character_resource_updates",
    "validate_effect_updates",
    "validate_item_instance_updates",
    "validate_runtime_effect_instance_updates",
    "validate_semantic_events",
    "validate_state_payload",
]
