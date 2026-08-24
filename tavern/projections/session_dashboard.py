from .common import *
from .dashboard import *

async def enrich_session_display_labels(
    database: Any,
    sessions: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Attach safe display fields to session-list DTO rows.

    This is a view-layer enrichment only.  It preserves the stored world state,
    reads the frozen package snapshot used by that session, and emits
    ``world_state.location_label`` plus an explicit resolution state.  Callers
    can therefore share one truthful projection across the overview, session
    list and live workbench without duplicating business logic in JavaScript.
    """

    result: list[dict[str, Any]] = []
    for raw in sessions:
        item = dict(raw) if isinstance(raw, Mapping) else {}
        session_id = _text(item.get("id"))
        state = (
            dict(item.get("world_state"))
            if isinstance(item.get("world_state"), Mapping)
            else {}
        )
        location_ref = _text(
            state.get("current_location")
            or state.get("location")
            or state.get("scene_id")
        )
        labels: Mapping[str, Any] = {}
        if session_id:
            try:
                labels = _world_labels(
                    await database.get_instance_config(session_id)
                )
            except Exception:
                labels = {}
        location_label = _world_display_label(location_ref, labels)
        state["location_label"] = location_label
        state["location_display_state"] = (
            "resolved"
            if location_label
            else ("missing" if not location_ref else "unresolved")
        )
        item["world_state"] = state
        result.append(item)
    return result


async def _session_id_labels(
    database: Any,
    session_id: str,
) -> dict[str, str]:
    """A16：副本内实体 id → 显示名映射（统一实体解析器）。

    覆盖参与者（id / uuid 后缀 / 群用户 ID / 名称）、玩家（user_id）、
    NPC（session_characters id / stable_key / uuid 后缀 / 名称）。
    前端在此基础上做后缀匹配与降级显示，不再把完整内部 ID 当普通名称展示。
    """
    from ..entity_resolver import strip_prefix

    labels: dict[str, str] = {}

    def add(key: Any, name: str) -> None:
        key = _text(key)
        name = _text(name)
        if key and name and key not in labels:
            labels[key] = name

    try:
        rows = await database.execute_read(
            """
            SELECT id, character_name, display_name, group_user_id
            FROM participants WHERE session_id = ?
            """,
            (session_id,),
        )
        for row in rows or []:
            pid = _text(row.get("id"))
            name = _text(row.get("character_name")) or _text(
                row.get("display_name")
            )
            if not name:
                continue
            add(pid, name)
            add(strip_prefix(pid), name)
            add(row.get("group_user_id"), name)
            add(name, name)
    except Exception:
        pass
    try:
        rows = await database.execute_read(
            """
            SELECT id, stable_key, name FROM session_characters
            WHERE session_id = ?
            """,
            (session_id,),
        )
        for row in rows or []:
            name = _text(row.get("name"))
            if not name:
                continue
            add(row.get("id"), name)
            add(row.get("stable_key"), name)
            add(strip_prefix(_text(row.get("id"))), name)
            add(name, name)
    except Exception:
        pass
    try:
        rows = await database.execute_read(
            """
            SELECT user_id, display_name, character_name FROM players
            WHERE session_id = ? AND enabled = 1
            """,
            (session_id,),
        )
        for row in rows or []:
            name = _text(row.get("display_name")) or _text(
                row.get("character_name")
            )
            add(row.get("user_id"), name)
    except Exception:
        pass
    # A16.3：世界包常驻角色名称也纳入解析（关系键可能直接用 NPC 名称）。
    try:
        snap = await database.execute_read(
            """
            SELECT world_snapshot_json FROM instance_configs
            WHERE session_id = ?
            """,
            (session_id,),
        )
        if snap:
            import json as _json

            snapshot = _json.loads(snap[0]["world_snapshot_json"] or "{}")
            for character in snapshot.get("characters") or []:
                if isinstance(character, dict):
                    add(character.get("name"), _text(character.get("name")))
                    add(f"npc:{character.get('name')}", _text(character.get("name")))
    except Exception:
        pass
    return labels


def _member_keys(item: Mapping[str, Any]) -> set[str]:
    """A19: 角色在 world_state 关系/背包中可能使用的 owner/source 候选键。"""
    from ..entity_resolver import strip_prefix

    keys: set[str] = set()
    for key in (
        item.get("id"),
        item.get("group_user_id"),
        item.get("user_id"),
        item.get("character_name"),
        item.get("character_code"),
        item.get("display_name"),
    ):
        key = _text(key)
        if key:
            keys.add(key)
            keys.add(strip_prefix(key))
    return keys


def _member_relationships(
    world_state: Mapping[str, Any],
    member_keys: set[str],
) -> list[dict[str, Any]]:
    """A19: 从 world_state.relationships（键为 ``来源→目标``）提取该角色的关系。

    同时保留 runtime_state.npc_relationships 作为补充来源（部分世界包/旧数据
    可能写入该字段），避免两种数据形态都丢失。
    """
    rows: list[dict[str, Any]] = []
    relationships = world_state.get("relationships") or {}
    if isinstance(relationships, Mapping):
        for raw_key, value in relationships.items():
            raw_key = _text(raw_key)
            if "→" not in raw_key:
                continue
            source, target = raw_key.split("→", 1)
            source = source.strip()
            target = _text(target).strip()
            if not source or source not in member_keys:
                continue
            if isinstance(value, Mapping):
                fields = {
                    _text(k): v
                    for k, v in value.items()
                    if isinstance(v, (int, float, str)) and v is not None
                }
                favor = value.get("好感")
                if not isinstance(favor, (int, float)):
                    favor = value.get("信任")
                if not isinstance(favor, (int, float)):
                    favor = next(
                        (v for v in value.values() if isinstance(v, (int, float))),
                        None,
                    )
                rows.append(
                    {
                        "target": target,
                        "favor": favor,
                        "stage": _text(
                            value.get("stage") or value.get("phase")
                        ),
                        "summary": _text(
                            value.get("summary") or value.get("note")
                        ),
                        "fields": fields,
                    }
                )
            elif isinstance(value, (int, float)):
                rows.append({"target": target, "favor": value, "fields": {}})
            elif isinstance(value, str):
                rows.append({"target": target, "summary": value, "fields": {}})
    return rows[:16]


def _normalize_reputation(value: Any) -> str:
    """A19: reputation 可能是标量或 dict，统一为可读文本。"""
    if isinstance(value, Mapping):
        parts = [
            f"{_text(k)} {v}"
            for k, v in value.items()
            if isinstance(v, (int, float, str)) and v is not None
        ]
        return " · ".join(parts) if parts else ""
    return _text(value)


def _entity_labels(world: Mapping[str, Any], entity_type: str) -> dict[str, str]:
    """Build display labels from the compiled entity index, never from ID suffixes."""

    labels: dict[str, str] = {}
    for raw in world.get("entity_index") or []:
        if not isinstance(raw, Mapping) or _text(raw.get("type")) != entity_type:
            continue
        label = _text(raw.get("label"))
        if not label:
            continue
        for key in (raw.get("id"), raw.get("short_ref"), raw.get("canonical_ref")):
            ref = _text(key)
            if ref:
                labels[ref] = label
    return labels


def _enrich_item_instances(
    rows: list[Mapping[str, Any]],
    world: Mapping[str, Any],
) -> list[dict[str, Any]]:
    labels = _entity_labels(world, "item")
    result: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        item = dict(raw)
        item_id = _text(item.get("item_id"))
        item["label"] = labels.get(item_id, "")
        if not item["label"]:
            item["display_error"] = "物品名称解析失败，请让管理员检查世界包物品目录。"
        result.append(item)
    return result


def _squad(
    roster_rows: list[Mapping[str, Any]],
    turn: Mapping[str, Any],
    world_state: Mapping[str, Any],
    world: Mapping[str, Any] | None = None,
    item_instances: Mapping[str, list[Mapping[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """A19: 小队列表——角色卡数值 / 背包 / 关系 / 状态 / 行动状态整合进每张角色卡。

    关系与背包以 ``world_state``（引擎权威数据）为准，runtime_state 作为补充，
    避免 A18 读取从不写入的 runtime 字段导致恒空。
    """
    world_state = world_state if isinstance(world_state, Mapping) else {}
    order = [
        item for item in (turn.get("order") or []) if isinstance(item, Mapping)
    ]
    position_by_user: dict[str, int] = {}
    for item in order:
        uid = _text(item.get("user_id"))
        if uid:
            position_by_user[uid] = _int(item.get("position"))
    current_user_id = _text(turn.get("current_user_id"))
    result: list[dict[str, Any]] = []
    for item in roster_rows:
        if not isinstance(item, Mapping):
            continue
        user_id = _text(item.get("group_user_id") or item.get("user_id"))
        runtime = item.get("runtime_state") or {}
        if not isinstance(runtime, Mapping):
            runtime = {}
        profile = item.get("card_profile") or item.get("draft_profile") or {}
        if not isinstance(profile, Mapping):
            profile = {}
        actor_view = project_actor_view(
            world or {},
            profile,
            viewer_role="admin",
        )
        semantic_values = actor_view.get("semantic_values") or {}
        stats = item.get("card_stats") or {}
        if not isinstance(stats, Mapping):
            stats = {}
        member_keys = _member_keys(item)
        rel_rows = _member_relationships(world_state, member_keys)
        if not rel_rows:
            # 补充：runtime_state.npc_relationships（旧数据/世界包自定义形态）
            npc_rel = runtime.get("npc_relationships") or {}
            if isinstance(npc_rel, Mapping):
                for target, value in list(npc_rel.items())[:16]:
                    if isinstance(value, Mapping):
                        rel_rows.append(
                            {
                                "target": _text(target),
                                "favor": value.get("favor")
                                if isinstance(value.get("favor"), (int, float))
                                else None,
                                "stage": _text(
                                    value.get("stage") or value.get("phase")
                                ),
                                "summary": _text(
                                    value.get("summary") or value.get("note")
                                ),
                                "fields": {},
                            }
                        )
                    elif isinstance(value, (int, float)):
                        rel_rows.append(
                            {"target": _text(target), "favor": value, "fields": {}}
                        )
                    elif isinstance(value, str):
                        rel_rows.append(
                            {"target": _text(target), "summary": value, "fields": {}}
                        )
        participant_id = _text(item.get("id"))
        owner_label = _text(
            actor_view.get("title")
            or item.get("character_name")
            or item.get("display_name"),
            "角色名称不可用",
        )
        member_resources = project_resource_view(
            world or {},
            item_instances=(item_instances or {}).get(participant_id, []),
            owner_labels={f"character:{participant_id}": owner_label},
            viewer_role="admin",
            include_technical_refs=True,
        )
        statuses = runtime.get("statuses") or []
        if not isinstance(statuses, list):
            statuses = []
        result.append(
            {
                "id": participant_id,
                "group_user_id": user_id,
                "character_name": _text(item.get("character_name")),
                "character_code": _text(item.get("character_code")),
                "display_name": _text(item.get("display_name")),
                "participation_status": _text(item.get("participation_status")),
                "card_status": _text(item.get("card_status")),
                "card_version": _int(item.get("card_version_no")),
                "card_review_note": _text(item.get("card_review_note")),
                "ready": _bool(item.get("ready")),
                "role": _text(
                    semantic_values.get("actor.identity.profession")
                    or semantic_values.get("actor.identity.species")
                ),
                "actor_view": actor_view,
                "resources": (
                    stats.get("raw")
                    if isinstance(stats.get("raw"), Mapping)
                    else stats
                ),
                "resource_labels": (
                    stats.get("labels")
                    if isinstance(stats.get("labels"), Mapping)
                    else {}
                ),
                "runtime": runtime,
                "statuses": statuses,
                "resource_view": member_resources,
                "relationships": rel_rows,
                "turn_position": position_by_user.get(user_id, 0),
                "is_current": bool(user_id and user_id == current_user_id),
                "current_location": _text(runtime.get("current_location")),
                "reputation": _normalize_reputation(runtime.get("reputation")),
            }
        )
    return result


async def _session_npcs(database: Any, session_id: str) -> list[dict[str, Any]]:
    """A18: 副本 NPC 卡片数据（世界包常驻 + 动态 NPC）。"""
    rows = await database.list_session_characters(session_id)
    result: list[dict[str, Any]] = []
    for item in rows or []:
        if not isinstance(item, Mapping):
            continue
        profile = item.get("public_profile") or {}
        if not isinstance(profile, Mapping):
            profile = {}
        state = item.get("state") or {}
        if not isinstance(state, Mapping):
            state = {}
        result.append(
            {
                "id": _text(item.get("id")),
                "stable_key": _text(item.get("stable_key")),
                "name": _text(item.get("name")),
                "role_type": _text(item.get("role_type")),
                "identity": _text(
                    profile.get("identity")
                    or profile.get("occupation")
                    or profile.get("class")
                ),
                "organization": _text(
                    profile.get("organization")
                    or profile.get("faction")
                    or profile.get("affiliation")
                ),
                "lifecycle_status": _text(item.get("lifecycle_status")),
                "source": _text(item.get("source")),
                "location": _text(
                    state.get("current_location") or state.get("location")
                ),
                "status": _text(state.get("status") or state.get("state")),
                "public_profile": profile,
                "state": state,
                "last_turn": _int(item.get("last_turn")),
            }
        )
    return result


__all__ = [name for name in globals() if not name.startswith('__')]

