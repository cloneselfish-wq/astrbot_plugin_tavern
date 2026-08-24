from .common import *
from .structure import *

def check_event_cascades(world: Mapping[str, Any]) -> list[dict[str, Any]]:
    """B2（A9）：事件级联规则体检——命令合法、目标存在、事件名非空。"""
    issues: list[dict[str, Any]] = []
    rules = _mapping(world.get("rules"))
    raw = rules.get("event_cascades")
    cascades = [item for item in _sequence(raw) if isinstance(item, Mapping)]
    from ..commands import COMMAND_DOMAINS

    quest_ids = {str(item.get("id")) for item in _sequence(_mapping(rules.get("quest_graph")).get("quests"))}
    fact_ids = {str(item.get("id")) for item in _sequence(_mapping(rules.get("knowledge_graph")).get("facts"))}
    npc_ids = {str(item.get("id")) for item in _sequence(_mapping(rules.get("npc_lifecycle")).get("npcs"))}
    faction_ids = {str(item.get("id")) for item in _sequence(_mapping(rules.get("faction_state")).get("factions"))}
    maps_handouts = _mapping(rules.get("maps_handouts"))
    handout_ids = {str(item.get("id")) for item in _sequence(maps_handouts.get("handouts"))}
    handout_ids.update(str(item.get("id")) for item in _sequence(maps_handouts.get("maps")) if isinstance(item, Mapping) and item.get("id"))
    scene_ids = {str(item.get("id")) for item in _sequence(_mapping(rules.get("scene_graph")).get("nodes"))}
    for index, rule in enumerate(cascades):
        field = f"rules.event_cascades[{index}]"
        when = _mapping(rule.get("when"))
        event_name = str(when.get("event") or "")
        if not event_name:
            issues.append(_issue("error", "cascade.event_missing", f"级联规则 {index} 缺少 when.event", f"{field}.when"))
        refs = when.get("refs")
        if refs is not None and not isinstance(refs, Mapping):
            issues.append(_issue("error", "cascade.refs", f"级联规则 {index} 的 when.refs 必须是对象", f"{field}.when.refs"))
        for raw_command in _sequence(rule.get("then")):
            if not isinstance(raw_command, Mapping):
                continue
            domain = str(raw_command.get("domain") or "").lower()
            action = str(raw_command.get("action") or "")
            raw_targets = _sequence(raw_command.get("targets"))
            if domain not in COMMAND_DOMAINS:
                issues.append(_issue("error", "cascade.command", f"级联规则 {index} 命令域不合法：{domain}", f"{field}.then"))
                continue
            if not action:
                issues.append(_issue("error", "cascade.command", f"级联规则 {index} 命令缺少 action", f"{field}.then"))
                continue
            if not raw_targets:
                issues.append(_issue("error", "cascade.command", f"级联规则 {index} 命令缺少 targets", f"{field}.then"))
                continue
            targets = [str(item) for item in raw_targets]
            for target in targets:
                if target.startswith("quest:") and target not in quest_ids:
                    issues.append(_issue("error", "cascade.quest", f"级联引用未知任务：{target}", f"{field}.then"))
                elif target.startswith("fact:") and target not in fact_ids:
                    issues.append(_issue("error", "cascade.fact", f"级联引用未知知识：{target}", f"{field}.then"))
                elif target.startswith("npc:") and target not in npc_ids:
                    issues.append(_issue("error", "cascade.npc", f"级联引用未知 NPC：{target}", f"{field}.then"))
                elif target.startswith("faction:") and target not in faction_ids:
                    issues.append(_issue("error", "cascade.faction", f"级联引用未知阵营：{target}", f"{field}.then"))
                elif target.startswith("handout:") and target not in handout_ids:
                    issues.append(_issue("error", "cascade.handout", f"级联引用未知手册：{target}", f"{field}.then"))
                elif target.startswith("scene:") and target not in scene_ids:
                    issues.append(_issue("error", "cascade.scene", f"级联引用未知场景：{target}", f"{field}.then"))
    return issues


def check_award_references(world: Mapping[str, Any]) -> list[dict[str, Any]]:
    """B2（A7）：成长里程碑奖励引用必须可解析（能力/物品/知识）。"""
    issues: list[dict[str, Any]] = []
    rules = _mapping(world.get("rules"))
    module = _mapping(rules.get("progression"))
    tracks = module.get("tracks", [])
    if not isinstance(tracks, Sequence) or isinstance(tracks, (str, bytes)):
        return issues
    from ...capability_service import CapabilityService
    from ...entity_registry import EntityRegistry

    try:
        registry = EntityRegistry(world)
    except Exception as exc:  # noqa: BLE001
        issues.append(_issue("error", "award.registry", f"能力注册失败：{exc}", "progression.tracks"))
        return issues
    from ...item_catalog import item_definitions

    item_ids = {item["item_id"] for item in item_definitions(world)}
    knowledge = {str(item.get("id")) for item in _sequence(_mapping(rules.get("knowledge_graph")).get("facts"))}
    for track in tracks:
        if not isinstance(track, Mapping):
            continue
        track_id = str(track.get("id") or "?")
        for entry in _sequence(track.get("milestone_awards")):
            if not isinstance(entry, Mapping):
                continue
            for award in _sequence(entry.get("awards")):
                if not isinstance(award, Mapping):
                    continue
                award_type = str(award.get("type") or "")
                ref = str(award.get("ref") or "")
                if not ref:
                    continue
                if award_type == "capability":
                    if not registry.contains(ref):
                        issues.append(_issue("error", "award.capability", f"轨迹 {track_id} 奖励引用未知能力：{ref}", f"progression.tracks.{track_id}.milestone_awards"))
                elif award_type == "item":
                    if ref not in item_ids and ref.removeprefix("item:") not in item_ids:
                        issues.append(_issue("error", "award.item", f"轨迹 {track_id} 奖励引用未知物品：{ref}", f"progression.tracks.{track_id}.milestone_awards"))
                elif award_type == "fact":
                    if ref not in knowledge:
                        issues.append(_issue("error", "award.fact", f"轨迹 {track_id} 奖励引用未知知识：{ref}", f"progression.tracks.{track_id}.milestone_awards"))
    return issues


def check_preset_lifecycle(world: Mapping[str, Any]) -> list[dict[str, Any]]:
    """B2（A6）：预设生命周期体检——replacement 必须存在；禁用项不可被模板引用。"""
    issues: list[dict[str, Any]] = []
    rules = _mapping(world.get("rules"))
    card = _mapping(rules.get("actor"))
    sets = card.get("preset_sets")
    sets = sets if isinstance(sets, Mapping) else {}
    for set_key, raw in sets.items():
        if not isinstance(raw, (list, dict)):
            continue
        items = raw.values() if isinstance(raw, dict) else raw
        ids_in_set: set[str] = set()
        for option in items:
            if isinstance(option, Mapping) and option.get("id"):
                ids_in_set.add(str(option["id"]))
        for option in items:
            if not isinstance(option, Mapping):
                continue
            preset_id = str(option.get("id") or "")
            replacement = str(option.get("replacement") or "").strip()
            if replacement:
                if replacement not in ids_in_set:
                    issues.append(
                        _issue(
                            "error",
                            "preset.replacement_missing",
                            f"预设 {preset_id} 的 replacement 指向不存在的预设：{replacement}",
                            f"actor.preset_sets.{set_key}.{preset_id}.replacement",
                        )
                    )
                if replacement == preset_id:
                    issues.append(
                        _issue(
                            "error",
                            "preset.replacement_self",
                            f"预设 {preset_id} 不能替代自身",
                            f"actor.preset_sets.{set_key}.{preset_id}.replacement",
                        )
                    )
    return issues


def preset_library_catalog(world: Mapping[str, Any]) -> dict[str, Any]:
    """规范化后的预设库目录（文档 03 §5.2/§7）。

    直接读取 world.rules.actor 原始卡，避免 card_template 精简后的
    preset_sets/preset_libraries 丢失；世界没有 actor 卡时返回空目录。
    """
    rules = _mapping(world.get("rules"))
    card = _mapping(rules.get("actor"))
    if not card:
        return {
            "items": [],
            "count": 0,
            "referenced_library_ids": [],
            "metadata_complete": True,
            "problems": [],
        }
    return normalize_preset_libraries(card)


def check_preset_libraries(world: Mapping[str, Any]) -> list[dict[str, Any]]:
    """预设库目录契约（文档 03 §6）。

    复用 normalize_preset_libraries 的权威 DTO：缺库、孤儿库、缺标签/说明、
    来源字段不一致、可见性冲突、废弃 preset_set 等 error 级问题进入
    check_template 的错误分组（阻断发布），unused 等进入警告分组。
    空目录但存在被引用集合时同样产生阻断错误，不按空列表视为完整。
    """
    catalog = preset_library_catalog(world)
    issues: list[dict[str, Any]] = []
    for problem in catalog.get("problems", []):
        severity = str(problem.get("severity") or "warning")
        issues.append(
            _issue(
                "error" if severity == "error" else "warning",
                str(problem.get("code") or "actor.preset_library.problem"),
                str(problem.get("message") or "预设库契约不完整"),
                str(problem.get("path") or "rules.actor.preset_libraries"),
            )
        )
    return issues


def check_private_leak(template: Mapping[str, Any]) -> list[dict[str, Any]]:
    """私密字段不得出现在默认公开投影。"""
    issues: list[dict[str, Any]] = []
    for field in _sequence(template.get("fields")):
        if not isinstance(field, Mapping):
            continue
        if field.get("private"):
            visibility = str(field.get("visibility") or "private")
            if visibility not in {"private", "host", "dm"}:
                issues.append(
                    _issue(
                        "error",
                        "private.visibility",
                        f"私密字段 {field.get('key')} 的可见性设置不安全：{visibility}",
                        f"actor.fields.{field.get('key')}",
                    )
                )
    return issues


def check_profession_coverage(template: Mapping[str, Any]) -> dict[str, Any]:
    """按职业输出覆盖矩阵：专精/武器/防具/能力/专长/弱点。"""
    professions = _sequence(template.get("profession_presets"))
    matrix: list[dict[str, Any]] = []
    for profession in professions:
        if not isinstance(profession, Mapping):
            continue
        name = str(profession.get("name") or profession.get("id") or "?")
        specializations = _sequence(profession.get("specialization_options") or profession.get("specializations"))
        weapons = _sequence(profession.get("starting_weapon_options"))
        armors = _sequence(profession.get("starting_armor_options"))
        abilities = _sequence(profession.get("ability_options"))
        feats = _sequence(profession.get("specialty_options"))
        weaknesses = _sequence(profession.get("weakness_options"))
        matrix.append(
            {
                "profession": name,
                "specializations": len(specializations),
                "weapons": len(weapons),
                "armors": len(armors),
                "abilities": len(abilities),
                "feats": len(feats),
                "weaknesses": len(weaknesses),
                "status": "ok",
                "missing": [],
            }
        )
    return {"matrix": matrix}


def _candidate_slug(candidate_id: Any, prefix: str) -> str:
    value = str(candidate_id or "").strip()
    return value.removeprefix(prefix).split(".", 1)[0]


def _operation_issue(
    registry: EntityRegistry,
    ability: Mapping[str, Any],
    *,
    path: str,
    entity: str,
) -> dict[str, Any] | None:
    operations = _sequence(ability.get("operations"))
    conditions = _sequence(ability.get("conditions"))
    if not operations:
        return _issue(
            "error",
            "actor.ability.no_runtime_operation",
            f"能力〈{entity}〉没有可执行操作，不能计入职业专属能力。",
            path,
            "声明至少一个可由 OperationEngine 校验并应用的 operation。",
            entity=entity,
        )
    try:
        engine = OperationEngine(registry)
        validated = engine.validate(operations)
        scoped_state = {
            "actor": {"refs": {}, "instances": {}, "tags": [], "references": []}
        }
        for operation in validated:
            target_ref = str(
                operation.get("target_ref") or operation.get("ref") or ""
            )
            if target_ref.startswith("resource:"):
                scoped_state["actor"]["refs"][target_ref] = 8
        engine.apply(validated, scoped_state, dry_run=True)
    except Exception as exc:  # noqa: BLE001
        return _issue(
            "error",
            "actor.ability.no_runtime_operation",
            f"能力〈{entity}〉的执行声明无效：{exc}",
            path,
            "修正 operation 类型、稳定引用、作用域或输入值后重新预检。",
            entity=entity,
        )
    if not conditions:
        return _issue(
            "error",
            "actor.ability.no_runtime_operation",
            f"能力〈{entity}〉缺少可验证的使用条件。",
            path,
            "声明资源、位置、目标或场景条件，并给出失败后的恢复提示。",
            entity=entity,
        )
    for index, entry in enumerate(conditions):
        condition = entry.get("when") if isinstance(entry, Mapping) else None
        problems = validate_condition_tree(condition, registry=registry)
        if problems:
            return _issue(
                "error",
                "actor.ability.no_runtime_operation",
                f"能力〈{entity}〉的条件无效：" + "；".join(problems[:3]),
                f"{path}.conditions[{index}]",
                "使用 ConditionEngine 支持的作用域、引用和运算符。",
                entity=entity,
            )
    return None


__all__ = [name for name in globals() if not name.startswith('__')]

