"""Host-independent opening-content contract."""

from __future__ import annotations

# 《第十三席》必须持续保有五个可选开局。
# 具体 ID 由作者源 opening_contract.required_opening_ids 声明，并由
# tools/requirements_guard.py 与开局契约测试共同防止后续迭代静默删减。

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class OpeningContractIssue:
    code: str
    opening_id: str
    scene_ref: str
    path: str
    message: str
    recovery: str = "请补齐世界作者源中的开场内容后重新构建世界包"

    def export(self) -> dict[str, str]:
        return {
            "code": self.code,
            "opening_id": self.opening_id,
            "scene_ref": self.scene_ref,
            "path": self.path,
            "message": self.message,
            "recovery": self.recovery,
        }


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def _text(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("text") or value.get("description") or "").strip()
    return str(value or "").strip()


def _known_refs(world: Mapping[str, Any]) -> set[str]:
    refs: set[str] = set()
    for item in _sequence(world.get("entity_index")):
        if not isinstance(item, Mapping):
            continue
        for key in ("canonical_ref", "short_ref"):
            value = str(item.get(key) or "").strip()
            if value:
                refs.add(value)
        entity_type = str(item.get("type") or "").strip()
        entity_id = str(item.get("id") or "").strip()
        if entity_type and entity_id:
            refs.add(f"{entity_type}:{entity_id}")
    actor = _mapping(_mapping(world.get("rules")).get("actor"))
    refs.update(
        str(item.get("id") or "").strip()
        for item in _sequence(actor.get("profession_presets"))
        if isinstance(item, Mapping)
    )
    return {item for item in refs if item}


def opening_contract_issues(
    world: Mapping[str, Any],
) -> list[OpeningContractIssue]:
    graph = _mapping(_mapping(world.get("rules")).get("scene_graph"))
    contract = _mapping(graph.get("opening_contract"))
    if not bool(contract.get("required")):
        return []
    openings = {
        str(item.get("id") or ""): item
        for item in _sequence(graph.get("opening_scenarios"))
        if isinstance(item, Mapping) and str(item.get("id") or "")
    }
    nodes_by_id: dict[str, list[Mapping[str, Any]]] = {}
    for item in _sequence(graph.get("nodes")):
        if isinstance(item, Mapping):
            nodes_by_id.setdefault(str(item.get("id") or ""), []).append(item)
    required = [
        str(item)
        for item in _sequence(contract.get("required_opening_ids"))
        if str(item).strip()
    ]
    minimum_hooks = int(contract.get("minimum_action_hooks") or 3)
    minimum_professions = int(
        contract.get("minimum_profession_entries") or 2
    )
    known_refs = _known_refs(world)
    issues: list[OpeningContractIssue] = []

    def add(
        code: str,
        opening_id: str,
        scene_ref: str,
        path: str,
        message: str,
    ) -> None:
        issues.append(
            OpeningContractIssue(
                code=code,
                opening_id=opening_id,
                scene_ref=scene_ref,
                path=path,
                message=message,
            )
        )

    for opening_id in required:
        opening = openings.get(opening_id)
        if opening is None:
            add(
                "opening_contract.required_opening_missing",
                opening_id,
                "",
                "rules.scene_graph.opening_scenarios",
                f"缺少必需开场：{opening_id}",
            )
            continue
        scene_ref = str(opening.get("scene_ref") or "").strip()
        matches = nodes_by_id.get(scene_ref, [])
        if len(matches) != 1:
            add(
                "opening_contract.missing_scene",
                opening_id,
                scene_ref,
                f"rules.scene_graph.nodes[{scene_ref}]",
                "开场场景不存在或定义不唯一",
            )
            continue
        node = matches[0]
        base = f"rules.scene_graph.nodes[{scene_ref}]"
        if not _text(node.get("opening_text")):
            add(
                "opening_contract.opening_text_missing",
                opening_id,
                scene_ref,
                f"{base}.opening_text",
                "开场正文缺失",
            )
        sensory = [
            item
            for item in _sequence(node.get("sensory_details"))
            if isinstance(item, Mapping)
            and str(item.get("id") or "")
            and _text(item.get("description"))
        ]
        if len(sensory) < 3:
            add(
                "opening_contract.sensory_details_insufficient",
                opening_id,
                scene_ref,
                f"{base}.sensory_details",
                "开场至少需要三条可感知细节",
            )
        conflict = _mapping(node.get("first_round_conflict"))
        if (
            not str(conflict.get("id") or "")
            or not _text(conflict.get("description"))
            or not _text(conflict.get("failure_forward"))
        ):
            add(
                "opening_contract.round1_missing",
                opening_id,
                scene_ref,
                f"{base}.first_round_conflict",
                "第一轮冲突或失败推进不完整",
            )
        objectives = [
            item
            for item in _sequence(node.get("objectives"))
            if isinstance(item, Mapping)
            and str(item.get("id") or "")
            and _text(item.get("description"))
        ]
        if len(objectives) < 2:
            add(
                "opening_contract.objectives_insufficient",
                opening_id,
                scene_ref,
                f"{base}.objectives",
                "开场至少需要两个可执行目标",
            )
        hooks = [
            item
            for item in _sequence(node.get("action_hooks"))
            if isinstance(item, Mapping)
            and str(item.get("id") or "")
            and _text(item.get("description"))
            and _text(item.get("limitations"))
        ]
        if len(hooks) < minimum_hooks:
            add(
                "opening_contract.action_hooks_insufficient",
                opening_id,
                scene_ref,
                f"{base}.action_hooks",
                f"开场至少需要 {minimum_hooks} 个包含限制的行动入口",
            )
        professions = [
            item
            for item in _sequence(node.get("profession_entries"))
            if isinstance(item, Mapping)
            and str(item.get("id") or "")
            and _text(item.get("description"))
            and _text(item.get("limitations"))
        ]
        if len(professions) < minimum_professions:
            add(
                "opening_contract.profession_entries_insufficient",
                opening_id,
                scene_ref,
                f"{base}.profession_entries",
                f"开场至少需要 {minimum_professions} 个包含限制的职业入口",
            )
        for index, entry in enumerate(professions):
            if str(entry.get("profession_ref") or "") not in known_refs:
                add(
                    "opening_contract.reference_missing",
                    opening_id,
                    scene_ref,
                    f"{base}.profession_entries[{index}].profession_ref",
                    "职业入口引用不存在",
                )
        for index, ref in enumerate(_sequence(node.get("npc_refs"))):
            if str(ref) not in known_refs:
                add(
                    "opening_contract.reference_missing",
                    opening_id,
                    scene_ref,
                    f"{base}.npc_refs[{index}]",
                    "开场 NPC 引用不存在",
                )
        for key in ("faction_ref", "clock_ref", "quest_ref"):
            if str(node.get(key) or "") not in known_refs:
                add(
                    "opening_contract.reference_missing",
                    opening_id,
                    scene_ref,
                    f"{base}.{key}",
                    f"开场引用不存在：{key}",
                )
        rhythm = _mapping(node.get("opening_rhythm"))
        if not all(
            isinstance(rhythm.get(key), Mapping)
            and _text(_mapping(rhythm.get(key)).get("description"))
            for key in ("round_1", "round_2", "round_3", "act_end")
        ):
            add(
                "opening_contract.rhythm_incomplete",
                opening_id,
                scene_ref,
                f"{base}.opening_rhythm",
                "开场三轮节奏与第一幕收束不完整",
            )
        stall = _mapping(node.get("stall_policy"))
        if (
            int(stall.get("turn_threshold") or 0) < 1
            or not _sequence(stall.get("meaningful_changes"))
            or not _sequence(stall.get("suggestions"))
            or not _text(stall.get("failure_forward"))
            or not _text(stall.get("delay_pressure"))
            or not _text(stall.get("inaction_beat"))
        ):
            add(
                "opening_contract.stall_policy_incomplete",
                opening_id,
                scene_ref,
                f"{base}.stall_policy",
                "开场停滞推进策略不完整",
            )
    return issues
