from __future__ import annotations

from .character import *

def actor_values_for_roles(
    world: Mapping[str, Any],
    profile: Mapping[str, Any] | None,
    roles: Sequence[str],
    *,
    viewer_role: str = "character",
) -> dict[str, str]:
    projected = project_actor_view(
        world,
        profile,
        viewer_role=viewer_role,
    )
    values = _mapping(projected.get("semantic_values"))
    return {str(role): str(values.get(str(role)) or "") for role in roles}


def _module_definition(world: Mapping[str, Any], module_id: str) -> dict[str, Any]:
    rules = _mapping(world.get("rules"))
    block = rules.get(module_id)
    if isinstance(block, Mapping):
        return dict(block)
    modules = _mapping(rules.get("modules"))
    block = modules.get(module_id)
    return dict(block) if isinstance(block, Mapping) else {}


def _definition_items(
    world: Mapping[str, Any], module_id: str, collection: str
) -> list[dict[str, Any]]:
    block = _module_definition(world, module_id)
    value = block.get(collection)
    if isinstance(value, Mapping):
        result: list[dict[str, Any]] = []
        for key, raw in value.items():
            item = dict(raw) if isinstance(raw, Mapping) else {}
            item.setdefault("id", str(key))
            result.append(item)
        return result
    return [dict(item) for item in _sequence(value) if isinstance(item, Mapping)]


def _runtime_collection(
    state: Mapping[str, Any] | None,
    module_id: str,
    collection: str,
) -> dict[str, Any]:
    source = _mapping(state)
    candidates: list[Any] = [source.get(collection)]
    for runtime_key in ("runtime", "v6_runtime"):
        runtime = _mapping(source.get(runtime_key))
        candidates.append(runtime.get(collection))
        modules = _mapping(runtime.get("modules"))
        candidates.append(_mapping(modules.get(module_id)).get(collection))
        candidates.append(modules.get(module_id))
    modules = _mapping(source.get("modules"))
    candidates.append(_mapping(modules.get(module_id)).get(collection))
    candidates.append(modules.get(module_id))
    for candidate in candidates:
        if isinstance(candidate, Mapping):
            return dict(candidate)
        if isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes)):
            result: dict[str, Any] = {}
            for raw in candidate:
                if not isinstance(raw, Mapping):
                    continue
                ref = str(raw.get("id") or raw.get("ref") or "").strip()
                if ref:
                    result[ref] = dict(raw)
            if result:
                return result
    return {}


def _state_definition(
    world: Mapping[str, Any], module_id: str, state_id: str
) -> dict[str, Any]:
    block = _module_definition(world, module_id)
    for key in (
        "states",
        "statuses",
        "status_definitions",
        "stances",
        "lifecycle_states",
        "conditions",
    ):
        source = block.get(key)
        if isinstance(source, Mapping):
            raw = source.get(state_id)
            if isinstance(raw, Mapping):
                return dict(raw)
            if raw not in (None, ""):
                return {"id": state_id, "label": str(raw)}
        for raw in _sequence(source):
            if not isinstance(raw, Mapping):
                continue
            identity = str(
                raw.get("id") or raw.get("key") or raw.get("value") or ""
            ).strip()
            if identity == state_id:
                return dict(raw)
    return {}


def resolve_state_label(
    world: Mapping[str, Any],
    *,
    module_id: str,
    state_id: Any,
    catalog: Mapping[str, str] | None = None,
    problems: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Resolve a runtime state to player-facing copy without echoing raw IDs."""

    identity = str(state_id or "").strip()
    issue_list = problems if problems is not None else []
    if not identity:
        return {
            "id": "",
            "label": "",
            "description": "",
            "available_actions": [],
            "terminal": False,
        }
    selected_catalog = dict(catalog or resolved_catalog(world)[0])
    definition = _state_definition(world, module_id, identity)
    root = str(definition.get("text_id") or "").strip()
    label = ""
    description = ""
    if root:
        label = str(selected_catalog.get(f"{root}.label") or "").strip()
        description = str(
            selected_catalog.get(f"{root}.description")
            or selected_catalog.get(f"{root}.summary")
            or ""
        ).strip()
    label = label or _friendly_mapping_text(definition, identity=identity)
    description = description or str(
        definition.get("description") or definition.get("summary") or ""
    ).strip()
    common = STATE_LABELS.get(module_id, {}).get(identity)
    if common:
        label = label or common[0]
        description = description or common[1]
    if not label:
        issue_list.append(
            {
                "code": "projection.state_unresolved",
                "path": f"{module_id}.states",
                "message": "状态名称解析失败",
            }
        )
        label = "状态名称解析失败"
    actions = [
        str(item)
        for item in _sequence(
            definition.get("available_actions") or definition.get("actions")
        )
        if str(item).strip()
    ]
    if not actions and module_id == "quest_graph":
        actions = {
            "available": ["activate"],
            "active": ["advance", "abandon"],
            "blocked": ["inspect"],
        }.get(identity, [])
    return {
        "id": identity,
        "label": label,
        "description": description,
        "available_actions": actions,
        "terminal": bool(definition.get("terminal"))
        or identity in {"completed", "failed", "abandoned", "dead", "archived"},
    }


def _definition_index(items: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for raw in items:
        identity = str(raw.get("id") or raw.get("ref") or "").strip()
        if identity:
            result[identity] = dict(raw)
    return result


def project_quest_view(
    world: Mapping[str, Any],
    quest_runtime: Mapping[str, Any] | None,
    *,
    viewer_role: str = "player",
    ledger: Sequence[Mapping[str, Any]] | None = None,
    include_technical_refs: bool = False,
) -> dict[str, Any]:
    role = str(viewer_role or "player").strip().lower()
    if role not in VIEWER_ROLES:
        raise ValueError(f"未知任务投影视角：{viewer_role}")
    diagnostic = bool(include_technical_refs and role in {"admin", "author"})
    problems: list[dict[str, Any]] = []
    catalog, locale, fallback = resolved_catalog(world)
    definitions = _definition_index(
        _definition_items(world, "quest_graph", "quests")
    )
    runtime = _runtime_collection(quest_runtime, "quest_graph", "quests")
    if not runtime and isinstance(quest_runtime, Mapping):
        runtime = dict(quest_runtime)
    rows: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    seen: set[str] = set()
    for ref, definition in definitions.items():
        current = _mapping(runtime.get(ref))
        rows.append((ref, definition, current))
        seen.add(ref)
    for ref, raw in runtime.items():
        if ref not in seen and isinstance(raw, Mapping):
            rows.append((str(ref), {}, dict(raw)))
            seen.add(str(ref))
    for index, raw in enumerate(ledger or ()):
        if not isinstance(raw, Mapping):
            continue
        ref = str(raw.get("quest_ref") or raw.get("id") or f"ledger:{index}")
        if ref in seen:
            continue
        definition = {
            "label": raw.get("title") or raw.get("label"),
            "description": raw.get("description"),
            "visibility": raw.get("visibility"),
            "objectives": raw.get("objectives") or [],
        }
        rows.append((ref, definition, dict(raw)))
        seen.add(ref)

    items: list[dict[str, Any]] = []
    for ref, definition, current in rows:
        visibility = str(
            current.get("visibility") or definition.get("visibility") or "public"
        ).lower()
        if visibility not in {"public", "player", "party"} and role not in PRIVATE_VIEWERS:
            continue
        label = str(
            definition.get("label")
            or definition.get("name")
            or current.get("label")
            or current.get("title")
            or ""
        ).strip()
        if not label or label == ref or _looks_internal_ref(label):
            label = _entity_label_index(world).get(ref, "")
        if not label:
            problems.append(
                {
                    "code": "projection.quest_label_missing",
                    "path": "quest_graph.quests",
                    "message": "任务名称解析失败",
                }
            )
        state_id = str(
            current.get("status")
            or current.get("state")
            or definition.get("state")
            or "available"
        )
        state = resolve_state_label(
            world,
            module_id="quest_graph",
            state_id=state_id,
            catalog=catalog,
            problems=problems,
        )
        objectives = [
            dict(item)
            for item in _sequence(definition.get("objectives"))
            if isinstance(item, Mapping)
        ]
        completed_refs = {
            str(item)
            for item in _sequence(
                current.get("completed_objectives")
                or current.get("completed")
            )
        }
        current_objectives = [
            str(item.get("label") or item.get("name") or "").strip()
            for item in objectives
            if str(item.get("id") or "") not in completed_refs
            and str(item.get("label") or item.get("name") or "").strip()
        ]
        item: dict[str, Any] = {
            "label": label,
            "summary": str(
                current.get("summary")
                or current.get("description")
                or definition.get("summary")
                or definition.get("description")
                or definition.get("settlement")
                or ""
            ).strip(),
            "status_id": state["id"],
            "status_label": state["label"],
            "status_description": state["description"],
            "completed_objectives": min(len(objectives), len(completed_refs)),
            "total_objectives": len(objectives),
            "current_objectives": current_objectives,
            "blocked_reason": str(
                current.get("blocked_reason") or current.get("blocker") or ""
            ).strip(),
            "recent_change": str(
                current.get("recent_change") or current.get("last_change") or ""
            ).strip(),
            "available_actions": state["available_actions"],
        }
        if diagnostic:
            item["quest_ref"] = ref
        items.append(item)
    return {
        "schema": "tavern-quest-view/1.0.0-rc10",
        "locale": locale,
        "locale_fallback": fallback,
        "items": items,
        "problems": problems,
    }


def project_faction_view(
    world: Mapping[str, Any],
    faction_runtime: Mapping[str, Any] | None,
    *,
    viewer_role: str = "player",
    include_technical_refs: bool = False,
) -> dict[str, Any]:
    role = str(viewer_role or "player").strip().lower()
    if role not in VIEWER_ROLES:
        raise ValueError(f"未知阵营投影视角：{viewer_role}")
    diagnostic = bool(include_technical_refs and role in {"admin", "author"})
    problems: list[dict[str, Any]] = []
    catalog, locale, fallback = resolved_catalog(world)
    definitions = _definition_index(
        _definition_items(world, "faction_state", "factions")
    )
    runtime = _runtime_collection(faction_runtime, "faction_state", "factions")
    if not runtime and isinstance(faction_runtime, Mapping):
        runtime = dict(faction_runtime)
    refs = list(definitions)
    refs.extend(ref for ref in runtime if ref not in definitions)
    labels = _entity_label_index(world)
    items: list[dict[str, Any]] = []
    for ref in refs:
        definition = definitions.get(ref, {})
        current = _mapping(runtime.get(ref))
        visibility = str(
            current.get("visibility") or definition.get("visibility") or "public"
        ).lower()
        if visibility not in {"public", "player", "party"} and role not in PRIVATE_VIEWERS:
            continue
        label = str(
            definition.get("label")
            or definition.get("name")
            or current.get("label")
            or labels.get(ref)
            or ""
        ).strip()
        if label == ref or _looks_internal_ref(label):
            label = labels.get(ref, "")
        if not label:
            problems.append(
                {
                    "code": "projection.faction_label_missing",
                    "path": "faction_state.factions",
                    "message": "阵营名称解析失败",
                }
            )
        stance_id = str(
            current.get("stance") or definition.get("stance") or "neutral"
        )
        stance = resolve_state_label(
            world,
            module_id="faction_state",
            state_id=stance_id,
            catalog=catalog,
            problems=problems,
        )
        item: dict[str, Any] = {
            "label": label,
            "stance_id": stance["id"],
            "stance_label": stance["label"],
            "stance_description": stance["description"],
            "summary": str(
                current.get("summary")
                or definition.get("summary")
                or definition.get("goal")
                or ""
            ).strip(),
            "recent_change": str(
                current.get("recent_change") or current.get("last_change") or ""
            ).strip(),
            "available_actions": stance["available_actions"],
        }
        if diagnostic:
            item["faction_ref"] = ref
        items.append(item)
    return {
        "schema": "tavern-faction-view/1.0.0-rc10",
        "locale": locale,
        "locale_fallback": fallback,
        "items": items,
        "problems": problems,
    }


def project_npc_view(
    world: Mapping[str, Any],
    npc_runtime: Mapping[str, Any] | None,
    *,
    session_npcs: Sequence[Mapping[str, Any]] | None = None,
    viewer_role: str = "player",
    include_technical_refs: bool = False,
) -> dict[str, Any]:
    role = str(viewer_role or "player").strip().lower()
    if role not in VIEWER_ROLES:
        raise ValueError(f"未知 NPC 投影视角：{viewer_role}")
    diagnostic = bool(include_technical_refs and role in {"admin", "author"})
    problems: list[dict[str, Any]] = []
    catalog, locale, fallback = resolved_catalog(world)
    definitions = _definition_index(
        _definition_items(world, "npc_lifecycle", "npcs")
    )
    runtime = _runtime_collection(npc_runtime, "npc_lifecycle", "npcs")
    labels = _entity_label_index(world)
    rows: dict[str, dict[str, Any]] = {
        ref: {"definition": definition, "runtime": _mapping(runtime.get(ref))}
        for ref, definition in definitions.items()
    }
    for ref, current in runtime.items():
        rows.setdefault(str(ref), {"definition": {}, "runtime": {}})["runtime"] = _mapping(current)
    for index, raw in enumerate(session_npcs or ()):
        if not isinstance(raw, Mapping):
            continue
        ref = str(
            raw.get("stable_key")
            or raw.get("npc_ref")
            or raw.get("id")
            or f"session-npc:{index}"
        )
        matched = rows.setdefault(ref, {"definition": {}, "runtime": {}})
        matched["session"] = dict(raw)
    items: list[dict[str, Any]] = []
    for ref, parts in rows.items():
        definition = _mapping(parts.get("definition"))
        current = _mapping(parts.get("runtime"))
        session = _mapping(parts.get("session"))
        state = _mapping(session.get("state"))
        profile = _mapping(session.get("public_profile"))
        label = str(
            session.get("name")
            or definition.get("name")
            or definition.get("label")
            or current.get("name")
            or labels.get(ref)
            or ""
        ).strip()
        if label == ref or _looks_internal_ref(label):
            label = labels.get(ref, "")
        if not label:
            problems.append(
                {
                    "code": "projection.npc_label_missing",
                    "path": "npc_lifecycle.npcs",
                    "message": "NPC 名称解析失败",
                }
            )
        presence_id = str(
            session.get("lifecycle_status")
            or current.get("lifecycle_status")
            or current.get("presence")
            or definition.get("initial_lifecycle")
            or definition.get("status")
            or "active"
        )
        presence = resolve_state_label(
            world,
            module_id="npc_presence",
            state_id=presence_id,
            catalog=catalog,
            problems=problems,
        )
        condition_id = str(
            state.get("condition")
            or state.get("status")
            or current.get("condition")
            or current.get("status")
            or ""
        ).strip()
        if condition_id in {"", "active", presence_id}:
            condition_id = ""
            condition = {"label": "", "description": ""}
        else:
            condition = resolve_state_label(
                world,
                module_id="npc_condition",
                state_id=condition_id,
                catalog=catalog,
                problems=problems,
            )
        location_ref = str(
            state.get("location")
            or state.get("scene")
            or current.get("scene")
            or current.get("location")
            or definition.get("initial_scene")
            or ""
        ).strip()
        location_label = labels.get(location_ref, "")
        item: dict[str, Any] = {
            "name": label,
            "role_label": str(
                session.get("role_type")
                or profile.get("role")
                or definition.get("role")
                or ""
            ).strip(),
            "presence_id": presence["id"],
            "presence_label": presence["label"],
            "condition_id": condition_id,
            "condition_label": str(condition.get("label") or ""),
            "location_label": location_label,
            "intent": str(
                state.get("intent")
                or current.get("intent")
                or profile.get("intent")
                or definition.get("intent")
                or ""
            ).strip(),
            "source_label": {
                "model_generated": "剧情生成 NPC",
                "world": "世界常驻 NPC",
                "builtin": "世界常驻 NPC",
                "manual": "管理员创建 NPC",
            }.get(str(session.get("source") or "world"), "世界常驻 NPC"),
        }
        if diagnostic:
            item["npc_ref"] = ref
            item["technical_ref"] = str(session.get("id") or ref)
        items.append(item)
    return {
        "schema": "tavern-npc-view/1.0.0-rc10",
        "locale": locale,
        "locale_fallback": fallback,
        "items": items,
        "problems": problems,
    }


def project_world_state_view(
    world: Mapping[str, Any],
    state: Mapping[str, Any] | None,
    *,
    ledger: Sequence[Mapping[str, Any]] | None = None,
    session_npcs: Sequence[Mapping[str, Any]] | None = None,
    viewer_role: str = "player",
    include_technical_refs: bool = False,
) -> dict[str, Any]:
    return {
        "schema": "tavern-world-state-view/1.0.0-rc10",
        "quest_view": project_quest_view(
            world,
            state,
            viewer_role=viewer_role,
            ledger=ledger,
            include_technical_refs=include_technical_refs,
        ),
        "faction_view": project_faction_view(
            world,
            state,
            viewer_role=viewer_role,
            include_technical_refs=include_technical_refs,
        ),
        "npc_view": project_npc_view(
            world,
            state,
            session_npcs=session_npcs,
            viewer_role=viewer_role,
            include_technical_refs=include_technical_refs,
        ),
    }


def world_capability_view(world: Mapping[str, Any]) -> dict[str, Any]:
    capability_index = _mapping(world.get("capability_index"))
    modules = [
        dict(item)
        for item in world.get("twp_modules") or []
        if isinstance(item, Mapping)
    ]
    return {
        "schema": "tavern-world-capabilities/1.0.0-rc10",
        "capabilities": [
            {"id": capability, "module_id": str(module_id)}
            for capability, module_id in sorted(capability_index.items())
        ],
        "modules": [
            {
                "id": str(item.get("module_id") or item.get("id") or ""),
                "enabled": bool(item.get("enabled")),
                "required": bool(item.get("required")),
                "absence_policy": str(
                    item.get("absence_policy") or "not_applicable"
                ),
                "capabilities": list(item.get("capabilities") or []),
            }
            for item in modules
        ],
    }


def world_has_capability(world: Mapping[str, Any], feature: str) -> bool:
    """Return whether an enabled world module provides a standard feature."""

    expected = RESOURCE_CAPABILITIES.get(str(feature or "").strip())
    if not expected:
        return False
    index = _mapping(world.get("capability_index"))
    if expected.intersection(str(key) for key in index):
        return True
    for raw in _sequence(world.get("twp_modules")):
        if not isinstance(raw, Mapping) or not bool(raw.get("enabled")):
            continue
        if expected.intersection(str(item) for item in _sequence(raw.get("capabilities"))):
            return True
    return False


def _owner_key(owner_type: Any, owner_ref: Any) -> str:
    kind = str(owner_type or "").strip()
    ref = str(owner_ref or "").strip()
    return f"{kind}:{ref}" if kind and ref else ""


def project_resource_view(
    world: Mapping[str, Any],
    *,
    item_instances: Sequence[Mapping[str, Any]] | None = None,
    economy: Mapping[str, Any] | None = None,
    owner_labels: Mapping[str, str] | None = None,
    viewer_role: str = "player",
    include_technical_refs: bool = False,
) -> dict[str, Any]:
    """Project inventory and wallets without leaking protocol identifiers.

    Callers must provide typed owner rows.  This function deliberately does not
    infer owners from display names or aliases such as ``party``/``team``.
    """

    role = str(viewer_role or "player").strip().lower()
    if role not in VIEWER_ROLES:
        raise ValueError(f"未知资源投影视角：{viewer_role}")
    diagnostic = bool(include_technical_refs and role in {"admin", "author"})
    inventory_enabled = world_has_capability(world, "inventory")
    economy_enabled = world_has_capability(world, "economy")
    labels = {
        str(key): str(value)
        for key, value in _mapping(owner_labels).items()
        if str(key).strip() and str(value).strip()
    }
    groups: dict[str, dict[str, Any]] = {}
    problems: list[dict[str, Any]] = []

    def owner_group(raw: Mapping[str, Any]) -> dict[str, Any] | None:
        owner_type = str(raw.get("owner_type") or "").strip()
        owner_ref = str(raw.get("owner_ref") or "").strip()
        key = _owner_key(owner_type, owner_ref)
        if not key:
            problems.append(
                {
                    "code": "projection.owner_ref_missing",
                    "message": "资源记录缺少类型化所有者引用，已跳过",
                }
            )
            return None
        if key not in groups:
            owner_label = str(
                labels.get(key)
                or raw.get("owner_label")
                or raw.get("owner_name")
                or ""
            ).strip()
            group: dict[str, Any] = {
                "kind": owner_type,
                "label": owner_label,
                "items": [],
                "wallets": [],
            }
            if not owner_label:
                group["display_error"] = "资源所有者名称缺失，请刷新后让管理员检查副本数据。"
                problems.append(
                    {
                        "code": "projection.owner_label_missing",
                        "message": "资源记录无法解析所有者显示名",
                    }
                )
            if diagnostic:
                group["technical_ref"] = {"owner_type": owner_type, "owner_ref": owner_ref}
            groups[key] = group
        return groups[key]

    if inventory_enabled:
        for raw in item_instances or ():
            if not isinstance(raw, Mapping):
                continue
            group = owner_group(raw)
            if group is None:
                continue
            item_label = str(raw.get("label") or raw.get("name") or "").strip()
            item: dict[str, Any] = {
                "label": item_label,
                "quantity": max(0, int(raw.get("quantity") or raw.get("count") or 0)),
                "category": str(raw.get("category") or ""),
                "description": str(raw.get("description") or ""),
                "container_label": str(raw.get("container_label") or ""),
            }
            if not item_label:
                item["display_error"] = "物品名称解析失败，请让管理员检查世界包物品目录。"
                problems.append(
                    {
                        "code": "projection.item_label_missing",
                        "message": "物品实例无法解析显示名",
                    }
                )
            if diagnostic:
                item["technical_ref"] = {
                    "item_id": str(raw.get("item_id") or raw.get("id") or ""),
                    "container": str(raw.get("container") or ""),
                }
            group["items"].append(item)

    economy_payload = _mapping(economy)
    currencies = {
        str(raw.get("currency_id") or ""): dict(raw)
        for raw in _sequence(economy_payload.get("currencies"))
        if isinstance(raw, Mapping) and str(raw.get("currency_id") or "")
    }
    if economy_enabled and bool(economy_payload.get("enabled")):
        for raw in _sequence(economy_payload.get("wallets")):
            if not isinstance(raw, Mapping):
                continue
            group = owner_group(raw)
            if group is None:
                continue
            currency_id = str(raw.get("currency_id") or "")
            currency = currencies.get(currency_id, {})
            currency_label = str(
                raw.get("label")
                or currency.get("label")
                or currency.get("name")
                or ""
            ).strip()
            wallet: dict[str, Any] = {
                "currency_label": currency_label,
                "short_label": str(
                    raw.get("short_label")
                    or currency.get("short_label")
                    or currency.get("short_name")
                    or ""
                ),
                "icon": str(raw.get("icon") or currency.get("icon") or ""),
                "balance": raw.get("amount", raw.get("balance", 0)),
                "formatted_balance": str(raw.get("formatted_amount") or ""),
            }
            if not currency_label:
                wallet["display_error"] = "货币名称解析失败，请让管理员检查世界包经济定义。"
                problems.append(
                    {
                        "code": "projection.currency_label_missing",
                        "message": "钱包记录无法解析货币显示名",
                    }
                )
            if diagnostic:
                wallet["technical_ref"] = {"currency_id": currency_id}
            group["wallets"].append(wallet)

    return {
        "schema": "tavern-resource-view/1.0.0-rc10",
        "capabilities": {
            "inventory": inventory_enabled,
            "economy": economy_enabled,
        },
        "owners": list(groups.values()),
        "problems": problems,
    }


def project_story_view(
    state: Mapping[str, Any] | None,
    *,
    world: Mapping[str, Any] | None = None,
    viewer_role: str = "player",
    include_technical_refs: bool = False,
) -> dict[str, Any]:
    source = _mapping(state)
    current_story = _mapping(source.get("current_story"))
    enabled = world_has_capability(world or {}, "story") if world is not None else True
    result: dict[str, Any] = {
        "schema": "tavern-story-view/1.0.0-rc10",
        "available": enabled,
        "title": str(current_story.get("title") or source.get("title") or "当前故事"),
        "content": str(
            current_story.get("content")
            or current_story.get("body")
            or source.get("content")
            or source.get("body")
            or source.get("scene_summary")
            or ""
        ),
        "turn": int(
            current_story.get("turn")
            or current_story.get("turn_no")
            or source.get("turn_number")
            or source.get("turn_no")
            or 0
        ),
        "source_label": str(current_story.get("source_label") or source.get("source_label") or ""),
        "generated_at": str(current_story.get("generated_at") or source.get("generated_at") or ""),
        "revision": int(current_story.get("revision") or source.get("revision") or 0),
    }
    role = str(viewer_role or "player").strip().lower()
    if include_technical_refs and role in {"admin", "author"}:
        result["technical_ref"] = {
            "event_id": str(current_story.get("event_id") or source.get("event_id") or ""),
            "seq": int(current_story.get("seq") or source.get("seq") or 0),
        }
    return result

__all__ = [name for name in globals() if not name.startswith('__')]
