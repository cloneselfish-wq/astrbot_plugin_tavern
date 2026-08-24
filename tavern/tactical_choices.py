"""Draft validation and visible-label binding for tactical choices."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .tactical_capability_runtime import (
    _capability_execution,
    _validate_frozen_item_use,
)
from .tactical_support import (
    ACTION_KINDS,
    _choice,
    _ref,
    _sequence,
    _state_refs,
    _zone_graph,
    _zone_reachable,
)

def draft_from_text(text: object, *, actor_key: str) -> dict[str, Any]:
    value = str(text or "").strip()
    if not value:
        raise ValueError("战术行动说明不能为空")
    hints = {
        "撤退": "retreat", "退": "retreat", "谈判": "parley", "交涉": "parley",
        "防守": "guard", "保护": "guard", "援助": "aid", "救援": "aid",
        "移动": "maneuver", "前往": "maneuver", "施法": "cast", "攻击": "strike",
    }
    action = next((kind for word, kind in hints.items() if word in value), "interact")
    return {
        "actor_key": str(actor_key or "").strip(),
        "action_kind": action,
        "description": value[:500],
        "target_refs": [],
        "zone_ref": "",
        "objective_ref": "",
        "capability_or_item_ref": "",
    }

def validate_draft(value: Mapping[str, Any]) -> dict[str, Any]:
    draft = dict(value)
    actor = str(draft.get("actor_key") or "").strip()
    action = str(draft.get("action_kind") or "").strip()
    if not actor or action not in ACTION_KINDS:
        raise ValueError("战术草稿缺少行动者或使用了未知行动")
    draft["actor_key"] = actor
    draft["action_kind"] = action
    draft["description"] = str(draft.get("description") or "").strip()[:500]
    draft["target_refs"] = [str(item).strip()[:160] for item in draft.get("target_refs") or [] if str(item).strip()][:8]
    draft["zone_ref"] = str(draft.get("zone_ref") or "").strip()[:160]
    draft["objective_ref"] = str(draft.get("objective_ref") or "").strip()[:160]
    draft["capability_or_item_ref"] = str(draft.get("capability_or_item_ref") or "").strip()[:160]
    return draft

def _visible_choice_rows(
    source: Any,
    *,
    ref_names: tuple[str, ...],
    kind: str,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if isinstance(source, Mapping):
        values = [(str(key), value) for key, value in source.items()]
    else:
        values = [("", value) for value in _sequence(source)]
    for source_ref, raw in values:
        if not isinstance(raw, Mapping):
            continue
        ref = next(
            (
                str(raw.get(name) or "").strip()
                for name in ref_names
                if str(raw.get(name) or "").strip()
            ),
            source_ref,
        )
        definition = raw.get("definition") or {}
        definition = definition if isinstance(definition, Mapping) else {}
        label = str(
            raw.get("label") or raw.get("name")
            or definition.get("label") or definition.get("name") or ""
        ).strip()[:120]
        if ref and label:
            rows.append({"ref": ref, "label": label, "kind": kind})
    return sorted(rows, key=lambda item: (item["label"], item["kind"], item["ref"]))

def _bind_visible_text_choice(
    description: str,
    candidates: list[dict[str, str]],
    *,
    label: str,
    required: bool,
    safe_default: str = "",
) -> str:
    text = str(description or "").strip()
    matches = [
        item for item in candidates
        if item["label"] and item["label"] in text
    ]
    if len(matches) == 1:
        return matches[0]["ref"]
    if len(matches) > 1:
        visible = "、".join(
            f"{item['kind']}「{item['label']}」" for item in matches
        )
        raise ValueError(f"行动说明同时命中多个{label}：{visible}；请明确一个可见名称")
    if safe_default and any(item["ref"] == safe_default for item in candidates):
        return safe_default
    if len(candidates) == 1:
        return candidates[0]["ref"]
    if required:
        visible = "、".join(
            f"{item['kind']}「{item['label']}」" for item in candidates[:12]
        )
        if visible:
            raise ValueError(f"行动说明未唯一匹配{label}；可见选项：{visible}")
        raise ValueError(f"当前没有可用的{label}")
    return ""

def prepare_draft(state: Mapping[str, Any], value: Mapping[str, Any]) -> dict[str, Any]:
    draft = validate_draft(value)
    refs = _state_refs(state)
    actor_key = draft["actor_key"]
    if actor_key not in refs["participants"]:
        raise ValueError("行动者不属于本场冲突的冻结参与者")
    actor = dict((state.get("participants") or {}).get(actor_key) or {})
    action = draft["action_kind"]
    description = str(draft.get("description") or "")
    if action in {"strike", "parley"} and not draft["target_refs"]:
        threat = _bind_visible_text_choice(
            description,
            _visible_choice_rows(
                state.get("known_threats") or state.get("threats"),
                ref_names=("threat_id", "id", "ref"),
                kind="威胁",
            ),
            label="威胁目标",
            required=True,
        )
        draft["target_refs"] = [threat]
    elif action in {"guard", "aid"} and not draft["target_refs"]:
        participant = _bind_visible_text_choice(
            description,
            _visible_choice_rows(
                state.get("participants"),
                ref_names=("actor_key", "user_id", "group_user_id", "id", "ref"),
                kind="队伍成员",
            ),
            label="队伍成员",
            required=True,
            safe_default=actor_key if action == "guard" else "",
        )
        draft["target_refs"] = [participant]
    if action in {"maneuver", "retreat"} and not draft["zone_ref"]:
        zone_source = (
            state.get("escape_routes")
            if action == "retreat"
            else state.get("zones")
        )
        draft["zone_ref"] = _bind_visible_text_choice(
            description,
            _visible_choice_rows(
                zone_source,
                ref_names=("zone_ref", "zone_id", "id", "ref"),
                kind="撤退出口" if action == "retreat" else "区域",
            ),
            label="撤退出口" if action == "retreat" else "区域",
            required=True,
        )
    if action == "interact" and not draft["objective_ref"]:
        draft["objective_ref"] = _bind_visible_text_choice(
            description,
            _visible_choice_rows(
                state.get("objectives"),
                ref_names=("id", "objective_id", "objective_ref", "ref"),
                kind="公开目标",
            ),
            label="公开目标",
            required=True,
        )

    if not draft["capability_or_item_ref"]:
        own_capabilities = [
            item for item in _sequence(state.get("available_capabilities"))
            if isinstance(item, Mapping)
            and str(item.get("owner_ref") or "") in {"", actor_key}
        ]
        own_items = [
            item for item in _sequence(state.get("available_items"))
            if isinstance(item, Mapping)
            and str(item.get("owner_ref") or "") in {"", actor_key}
        ]
        draft["capability_or_item_ref"] = _bind_visible_text_choice(
            description,
            [
                *_visible_choice_rows(
                    own_capabilities,
                    ref_names=("id", "capability_id", "ref"),
                    kind="能力",
                ),
                *_visible_choice_rows(
                    own_items,
                    ref_names=("id", "item_id", "ref"),
                    kind="装备",
                ),
            ],
            label="能力或装备",
            required=action == "cast",
        )

    unknown_targets = set(draft["target_refs"]) - (refs["participants"] | refs["threats"])
    if unknown_targets:
        raise ValueError("战术目标已失效或不属于当前冲突")
    if action in {"strike", "parley"} and any(ref not in refs["threats"] for ref in draft["target_refs"]):
        raise ValueError("攻击或谈判只能选择本场已知威胁")
    if action in {"guard", "aid"} and any(ref not in refs["participants"] for ref in draft["target_refs"]):
        raise ValueError("防守或援助只能选择本场冻结参与者")
    if draft["zone_ref"] and draft["zone_ref"] not in refs["zones"]:
        raise ValueError("战术区域已失效或不可到达")
    if action == "maneuver" and draft["zone_ref"] != str(actor.get("zone_ref") or ""):
        graph = _zone_graph(state)
        if state.get("zone_edges") and draft["zone_ref"] not in graph.get(str(actor.get("zone_ref") or ""), set()):
            raise ValueError("移动只能选择与当前区域直接相连的区域")
    if action == "retreat" and draft["zone_ref"]:
        exits = {
            _ref(dict(item), "zone_ref", "zone_id", "id", "ref")
            for item in _sequence(state.get("escape_routes"))
            if isinstance(item, Mapping)
        }
        if exits and draft["zone_ref"] not in exits:
            raise ValueError("撤退只能选择公开撤退出口")
        if not _zone_reachable(state, str(actor.get("zone_ref") or ""), draft["zone_ref"]):
            raise ValueError("所选撤退出口与当前区域不连通")
    if draft["objective_ref"] and draft["objective_ref"] not in refs["objectives"]:
        raise ValueError("公开目标已失效或不属于当前冲突")
    capability = draft["capability_or_item_ref"]
    if capability and capability not in refs["capabilities"] | refs["items"]:
        raise ValueError("能力或物品未在本场冲突中授权")
    if capability:
        choice_kind, authorized = _choice(state, capability)
        owner_ref = str(authorized.get("owner_ref") or "").strip()
        if owner_ref and owner_ref != actor_key:
            raise ValueError("不能使用另一位参与者持有的能力或物品")
        if choice_kind == "item":
            _validate_frozen_item_use(authorized, action)
        elif choice_kind == "capability":
            _capability_execution(
                state,
                draft,
                authorized,
                success=True,
                lock_key="preview",
            )
    if action in {"strike", "parley"} and not draft["target_refs"]:
        raise ValueError("该行动需要选择一个已知威胁")
    if action in {"maneuver", "retreat"} and not draft["zone_ref"]:
        raise ValueError("该行动需要选择一个可达区域")
    if action == "interact" and not draft["objective_ref"]:
        raise ValueError("处理目标需要选择一个公开 objective")
    if action == "cast" and not capability:
        raise ValueError("施展能力需要选择已授权能力或物品")
    return draft
