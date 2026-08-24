"""Unified human and AI-companion TeamStrip projection."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..projections.character import project_actor_view
from .common import (
    display_label,
    integer,
    latest_timestamp,
    mapping,
    number_or_none,
    text,
)
from .keys import OpaqueKeyFactory


def _item_labels(world: Mapping[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in world.get("entity_index") or ():
        if not isinstance(raw, Mapping) or text(raw.get("type")) != "item":
            continue
        label = display_label(raw.get("label") or raw.get("name"))
        if not label:
            continue
        for candidate in (
            raw.get("id"),
            raw.get("short_ref"),
            raw.get("canonical_ref"),
        ):
            ref = text(candidate, limit=180)
            if ref:
                result[ref] = label
    return result


def _inventories(
    rows: Sequence[Mapping[str, Any]] | None,
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for raw in rows or ():
        if not isinstance(raw, Mapping):
            continue
        owner = text(raw.get("owner_ref"), limit=180)
        if owner:
            result.setdefault(owner, []).append(dict(raw))
    return result


def _inventory_summary(
    rows: Sequence[Mapping[str, Any]],
    labels: Mapping[str, str],
    *,
    linked: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not linked:
        return (
            {
                "state": "unknown",
                "count": None,
                "items": [],
                "has_more": False,
                "summary": "当前数据没有可验证的 AI 队友背包关联。",
                "updated_at": "",
            },
            [
                {
                    "code": "visual.party.ai_inventory_relation_missing",
                    "message": "AI 队友尚无可验证的背包关联。",
                    "recovery": "系统不会猜测背包归属；请由后端补齐关联后刷新。",
                    "retryable": False,
                }
            ],
        )
    items: list[dict[str, Any]] = []
    problems: list[dict[str, Any]] = []
    for raw in rows:
        label = display_label(
            raw.get("label")
            or raw.get("name")
            or labels.get(text(raw.get("item_id"), limit=180))
        )
        if not label:
            problems.append(
                {
                    "code": "visual.party.inventory_label_missing",
                    "message": "一件物品缺少可读名称，已从摘要中跳过。",
                    "recovery": "请让管理员检查世界物品目录。",
                    "retryable": False,
                }
            )
            continue
        items.append(
            {
                "label": label,
                "quantity": max(0, integer(raw.get("quantity"), 0)),
            }
        )
    return (
        {
            "state": "ready" if items else "empty",
            "count": len(items),
            "items": items[:3],
            "has_more": len(items) > 3,
            "summary": (
                f"共 {len(items)} 类物品" if items else "当前没有可见物品"
            ),
            "updated_at": latest_timestamp(
                *(raw.get("updated_at") for raw in rows)
            ),
        },
        problems,
    )


def _actor_resources(
    actor_view: Mapping[str, Any],
    stats: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    resources: list[dict[str, Any]] = []
    stats = mapping(stats)
    raw_values = mapping(stats.get("raw")) or stats
    labels = mapping(stats.get("labels"))
    for key, value in raw_values.items():
        numeric = number_or_none(value)
        label = display_label(labels.get(str(key)))
        if label and numeric is not None:
            resources.append({"label": label, "value": numeric, "max": None})
        if len(resources) >= 3:
            return resources
    for section in actor_view.get("sections") or ():
        if not isinstance(section, Mapping):
            continue
        for raw in section.get("items") or ():
            if not isinstance(raw, Mapping):
                continue
            role = text(raw.get("role"), limit=80).lower()
            if not any(token in role for token in ("stat", "resource", "health")):
                continue
            label = display_label(raw.get("label"))
            value = text(raw.get("display_value"), limit=80)
            if label and value:
                resources.append({"label": label, "value": value, "max": None})
            if len(resources) >= 3:
                return resources
    return resources


def _resource_metadata(
    world: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for raw in world.get("entity_index") or ():
        if not isinstance(raw, Mapping):
            continue
        kind = text(raw.get("type"), limit=40).lower()
        ref = text(raw.get("id"), limit=180)
        if kind not in {"resource", "counter", "stat", "attribute"} and not ref.startswith(
            ("resource:", "counter:", "stat:", "attribute:")
        ):
            continue
        visibility = text(raw.get("visibility"), limit=30, default="public")
        if visibility not in {"", "public", "player", "party", "group"}:
            continue
        label = display_label(raw.get("label") or raw.get("name"))
        if not ref or not label:
            continue
        bounds = mapping(raw.get("range"))
        maximum = number_or_none(
            bounds.get("max")
            if bounds.get("max") is not None
            else raw.get("max")
        )
        for candidate in (
            raw.get("id"),
            raw.get("short_ref"),
            raw.get("canonical_ref"),
        ):
            candidate_ref = text(candidate, limit=180)
            if candidate_ref:
                result[candidate_ref] = {"label": label, "max": maximum}
    return result


def _runtime_resources(
    runtime: Mapping[str, Any],
    world: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Project current actor resources without inventing labels or zeroes."""

    raw_resources = runtime.get("resources")
    if isinstance(raw_resources, Mapping):
        source = [
            ({"label": key, "value": value} if not isinstance(value, Mapping)
             else {"label": key, **dict(value)})
            for key, value in raw_resources.items()
        ]
    elif isinstance(raw_resources, Sequence) and not isinstance(
        raw_resources, (str, bytes)
    ):
        source = list(raw_resources)
    else:
        source = []
    resources: list[dict[str, Any]] = []
    for raw in source:
        if not isinstance(raw, Mapping):
            continue
        visibility = text(raw.get("visibility"), limit=30, default="public")
        if visibility not in {"", "public", "player", "party", "group"}:
            continue
        label = display_label(raw.get("label") or raw.get("name"))
        value = number_or_none(
            raw.get("current") if raw.get("current") is not None else raw.get("value")
        )
        maximum_value = (
            raw.get("max")
            if raw.get("max") is not None
            else raw.get("maximum")
        )
        maximum = number_or_none(maximum_value)
        if label and value is not None:
            resources.append({"label": label, "value": value, "max": maximum})
        if len(resources) >= 3:
            break
    metadata = _resource_metadata(world)
    for ref, value in mapping(runtime.get("refs")).items():
        definition = metadata.get(str(ref))
        numeric = number_or_none(value)
        if definition is None or numeric is None:
            continue
        resources.append(
            {
                "label": definition["label"],
                "value": numeric,
                "max": definition["max"],
            }
        )
        if len(resources) >= 3:
            break
    return resources


def _merged_resources(
    runtime: Mapping[str, Any],
    actor_view: Mapping[str, Any],
    world: Mapping[str, Any],
    stats: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in (
        *_runtime_resources(runtime, world),
        *_actor_resources(actor_view, stats),
    ):
        label = display_label(item.get("label"))
        identity = label.casefold()
        if not label or identity in seen:
            continue
        seen.add(identity)
        result.append(dict(item))
        if len(result) >= 3:
            break
    return result


def _ai_action_state(
    *,
    action_status: str,
    mode: str,
    pending: bool,
    current: bool,
) -> str:
    """Translate persistence states into stable player-facing activity states."""

    if pending:
        return "awaiting_confirmation"
    if mode == "paused" or action_status == "paused":
        return "paused"
    if action_status == "error":
        return "blocked"
    if action_status == "retry_wait":
        return "recovering"
    if action_status == "acting":
        return "acting"
    if action_status == "waiting":
        return "waiting"
    if current:
        return "acting"
    return "ready"


def _actor_capabilities(actor_view: Mapping[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for section in actor_view.get("sections") or ():
        if not isinstance(section, Mapping):
            continue
        section_id = text(section.get("id"), limit=60).lower()
        for raw in section.get("items") or ():
            if not isinstance(raw, Mapping):
                continue
            role = text(raw.get("role"), limit=80).lower()
            if not any(
                token in (section_id + " " + role)
                for token in ("skill", "ability", "capability", "talent")
            ):
                continue
            label = display_label(raw.get("label"))
            value = text(raw.get("display_value"), limit=80)
            if label:
                result.append({"label": label, "summary": value})
            if len(result) >= 3:
                return result
    return result


def _public_statuses(
    runtime: Mapping[str, Any],
    ui_profile: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    taxonomy = {
        text(item.get("role_handle"), limit=100): dict(item)
        for item in mapping(ui_profile).get("status_taxonomy") or ()
        if isinstance(item, Mapping)
        and text(item.get("role_handle"), limit=100)
    }
    result: list[dict[str, Any]] = []
    for raw in runtime.get("statuses") or ():
        if isinstance(raw, Mapping):
            visibility = text(raw.get("visibility"), limit=30, default="public")
            if visibility not in {"", "public", "player", "party", "group"}:
                continue
            label = display_label(raw.get("label") or raw.get("name"))
            summary = text(raw.get("summary") or raw.get("description"), limit=100)
            handle = text(
                raw.get("role_handle") or raw.get("semantic_handle"),
                limit=100,
            )
            presentation = taxonomy.get(handle, {})
            tone = text(
                presentation.get("tone") or raw.get("tone"),
                limit=30,
                default="neutral",
            )
            symbol = text(
                presentation.get("symbol") or raw.get("symbol"),
                limit=30,
                default="dot",
            )
            duration = text(
                raw.get("duration_label") or raw.get("duration"),
                limit=60,
            )
        else:
            label = display_label(raw)
            summary = ""
            tone = "neutral"
            symbol = "dot"
            duration = ""
        if label:
            result.append(
                {
                    "label": label,
                    "summary": summary,
                    "tone": tone,
                    "symbol": symbol,
                    "duration": duration,
                }
            )
        if len(result) >= 2:
            break
    return result


def _identity_facets(
    actor_view: Mapping[str, Any],
    ui_profile: Mapping[str, Any],
) -> list[dict[str, Any]]:
    semantic_values = mapping(actor_view.get("semantic_values"))
    role_index = mapping(ui_profile.get("public_field_roles"))
    values_by_handle = {
        text(mapping(meta).get("handle"), limit=100): display_label(
            semantic_values.get(role)
        )
        for role, meta in role_index.items()
        if text(mapping(meta).get("handle"), limit=100)
    }
    facets = []
    for raw in mapping(ui_profile.get("party")).get("identity_facets") or ():
        facet = mapping(raw)
        handle = text(facet.get("role_handle"), limit=100)
        value = values_by_handle.get(handle, "")
        label = display_label(facet.get("label"))
        if handle and label and value:
            facets.append(
                {
                    "role_handle": handle,
                    "label": label,
                    "value": value,
                    "priority": integer(facet.get("priority"), 100),
                }
            )
    return sorted(facets, key=lambda item: item["priority"])


def _adaptive_attributes(
    stats: Mapping[str, Any] | None,
    world: Mapping[str, Any],
    ui_profile: Mapping[str, Any],
) -> list[dict[str, Any]]:
    stats_map = mapping(stats)
    raw_values = mapping(stats_map.get("raw")) or stats_map
    labels = mapping(stats_map.get("labels"))
    definitions = {
        text(item.get("key"), limit=80): dict(item)
        for item in mapping(world.get("actor")).get("stats", {}).get("attributes", [])
        if isinstance(item, Mapping) and text(item.get("key"), limit=80)
    }
    requested: list[str] = []
    for visual in ui_profile.get("visualizations") or ():
        if not isinstance(visual, Mapping):
            continue
        for handle in visual.get("role_handles") or ():
            normalized = text(handle, limit=100)
            if normalized.startswith("actor-attribute:") and normalized not in requested:
                requested.append(normalized)
    result = []
    for handle in requested:
        key = handle.split(":", 1)[1]
        value = number_or_none(raw_values.get(key))
        definition = definitions.get(key, {})
        label = display_label(labels.get(key) or definition.get("label"))
        if value is None or not label:
            continue
        result.append(
            {
                "role_handle": handle,
                "label": label,
                "value": value,
                "min": number_or_none(definition.get("minimum")),
                "max": number_or_none(definition.get("maximum")),
            }
        )
    return result


def _attribute_view(
    attributes: Sequence[Mapping[str, Any]],
    ui_profile: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Bind public axes to the first declared visualization with real data."""

    by_handle = {
        text(item.get("role_handle"), limit=100): dict(item)
        for item in attributes
        if isinstance(item, Mapping)
        and text(item.get("role_handle"), limit=100)
    }
    for raw in ui_profile.get("visualizations") or ():
        visual = mapping(raw)
        handles = [
            text(item, limit=100)
            for item in visual.get("role_handles") or ()
            if text(item, limit=100)
        ]
        axes = [by_handle[handle] for handle in handles if handle in by_handle]
        if not axes:
            continue
        scale = mapping(visual.get("scale"))
        kind = text(visual.get("kind"), limit=30, default="list")
        if kind not in {"radar", "bars", "list"}:
            kind = "list"
        fallback = text(visual.get("fallback"), limit=30, default="list")
        if fallback not in {"bars", "list"}:
            fallback = "list"
        return {
            "kind": kind,
            "title": display_label(visual.get("title")),
            "axes": axes,
            "scale": {
                "min": number_or_none(scale.get("min")),
                "max": number_or_none(scale.get("max")),
                "unit": text(scale.get("unit"), limit=24),
            },
            "fallback": fallback,
        }
    return None


def _detail_sections(
    *,
    ui_profile: Mapping[str, Any],
    attribute_view: Mapping[str, Any] | None,
    resources: Sequence[Mapping[str, Any]],
    statuses: Sequence[Mapping[str, Any]],
    inventory: Mapping[str, Any],
    capabilities: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Return only declared sections backed by a safe actor projection."""

    declared = [
        text(item, limit=40)
        for item in mapping(ui_profile.get("actor_detail")).get("sections") or ()
        if text(item, limit=40)
    ]
    party = mapping(ui_profile.get("party"))
    available = {"identity"}
    if attribute_view and attribute_view.get("axes"):
        available.add("attributes")
    if mapping(party.get("resources")) and resources:
        available.add("resources")
    if mapping(party.get("statuses")) and statuses:
        available.add("statuses")
    if mapping(party.get("inventory")) and text(
        inventory.get("state"), limit=30
    ) in {"ready", "empty", "unknown"}:
        available.add("inventory")
    if capabilities:
        available.add("capabilities")
    return [section for section in declared if section in available]


def project_party(
    *,
    roster: Sequence[Mapping[str, Any]] | None,
    companions: Sequence[Mapping[str, Any]] | None,
    turn: Mapping[str, Any] | None,
    world: Mapping[str, Any],
    item_instances: Sequence[Mapping[str, Any]] | None,
    inventory_available: bool = True,
    viewer_role: str,
    keys: OpaqueKeyFactory,
    ui_profile: Mapping[str, Any] | None = None,
    limit: int = 8,
) -> dict[str, Any]:
    role = text(viewer_role, limit=30, default="player").lower()
    ui_profile = mapping(ui_profile)
    privileged = role in {"dm", "admin"}
    turn = mapping(turn)
    order = [item for item in turn.get("order") or () if isinstance(item, Mapping)]
    order_by_ref: dict[str, dict[str, Any]] = {}
    for item in order:
        for candidate in (item.get("user_id"), item.get("actor_ref")):
            ref = text(candidate, limit=180)
            if ref:
                order_by_ref[ref] = dict(item)
    current_ref = text(turn.get("current_user_id"), limit=180)
    inventories = _inventories(item_instances)
    labels = _item_labels(world)
    items: list[dict[str, Any]] = []
    problems: list[dict[str, Any]] = []

    for index, raw in enumerate(roster or ()):
        if not isinstance(raw, Mapping):
            continue
        participation = text(raw.get("participation_status"), limit=30)
        card_status = text(raw.get("card_status"), limit=30)
        if participation in {"retired", "removed", "rejected"}:
            continue
        if not privileged and card_status not in {"", "approved"}:
            continue
        profile = raw.get("card_profile") or raw.get("draft_profile") or {}
        actor_view = project_actor_view(
            world,
            profile if isinstance(profile, Mapping) else {},
            viewer_role=role,
            include_technical_refs=False,
        )
        name = display_label(
            actor_view.get("title")
            or raw.get("character_name")
            or raw.get("display_name"),
            fallback="角色名称不可用",
        )
        participant_ref = text(raw.get("id"), limit=180)
        user_ref = text(
            raw.get("group_user_id") or raw.get("user_id"), limit=180
        )
        order_item = order_by_ref.get(user_ref, {})
        inventory, inventory_problems = _inventory_summary(
            inventories.get(participant_ref, []), labels, linked=True
        )
        problems.extend(inventory_problems)
        attributes = _adaptive_attributes(
            mapping(raw.get("card_stats")), world, ui_profile
        )
        resources = _actor_resources(
            actor_view, mapping(raw.get("card_stats"))
        )
        statuses = _public_statuses(
            mapping(raw.get("runtime_state")), ui_profile
        )
        capabilities = _actor_capabilities(actor_view)
        attribute_view = _attribute_view(attributes, ui_profile)
        actor = {
            "key": keys.key("teammate", f"human:{participant_ref or index}"),
            "kind": "human",
            "name": name,
            "action_state": "current" if user_ref == current_ref else "waiting",
            "is_current": bool(user_ref and user_ref == current_ref),
            "turn_position": integer(order_item.get("position"), 0),
            "participation_state": participation or "active",
            "identity_facets": _identity_facets(actor_view, ui_profile),
            "attributes": attributes,
            "statuses": statuses,
            "resources": resources,
            "inventory": inventory,
            "capabilities": capabilities,
            "detail_sections": _detail_sections(
                ui_profile=ui_profile,
                attribute_view=attribute_view,
                resources=resources,
                statuses=statuses,
                inventory=inventory,
                capabilities=capabilities,
            ),
            "updated_at": latest_timestamp(
                raw.get("updated_at"), inventory.get("updated_at")
            ),
        }
        if attribute_view:
            actor["attribute_view"] = attribute_view
        items.append(actor)

    for index, raw in enumerate(companions or ()):
        if not isinstance(raw, Mapping):
            continue
        actor_status = text(raw.get("status"), limit=30, default="active")
        if actor_status in {"retired", "archived"}:
            continue
        public_actor_ref = text(raw.get("actor_ref"), limit=180)
        profile = raw.get("profile") if isinstance(raw.get("profile"), Mapping) else {}
        runtime = mapping(raw.get("state"))
        actor_view = project_actor_view(
            world,
            profile,
            viewer_role=role,
            include_technical_refs=False,
        )
        name = display_label(
            raw.get("display_name") or actor_view.get("title"),
            fallback="AI 队友名称不可用",
        )
        order_item = order_by_ref.get(public_actor_ref, {})
        mode = text(raw.get("mode"), limit=30, default="confirm")
        action_status = text(
            raw.get("action_status"), limit=30, default=actor_status
        )
        pending = bool(raw.get("awaiting_confirmation"))
        is_current = bool(public_actor_ref and public_actor_ref == current_ref)
        action_state = _ai_action_state(
            action_status=action_status,
            mode=mode,
            pending=pending,
            current=is_current,
        )
        linked_inventory = bool(
            inventory_available
            and (
                raw.get("inventory_supported")
                or public_actor_ref in inventories
            )
        )
        inventory, inventory_problems = _inventory_summary(
            inventories.get(public_actor_ref, []),
            labels,
            linked=linked_inventory,
        )
        problems.extend(inventory_problems)
        if not profile:
            problems.append(
                {
                    "code": "visual.party.ai_profile_unavailable",
                    "message": "一名 AI 队友的角色资料暂时不可用。",
                    "recovery": "请刷新小队板块；系统不会猜测角色状态。",
                    "retryable": True,
                }
            )
        attributes = _adaptive_attributes(
            mapping(profile.get("stats")), world, ui_profile
        )
        statuses = _public_statuses(runtime, ui_profile)
        resources = _merged_resources(
            runtime,
            actor_view,
            world,
            mapping(profile.get("stats")),
        )
        capability_items = _actor_capabilities(actor_view)
        capabilities = {
            "can_take_turn": (
                mode != "paused"
                and actor_status == "active"
                and action_status not in {"paused", "error"}
            ),
            "requires_confirmation": mode == "confirm",
            "can_manage": privileged,
            "items": capability_items,
        }
        attribute_view = _attribute_view(attributes, ui_profile)
        actor = {
            "key": keys.key("teammate", f"ai:{public_actor_ref or index}"),
            "kind": "ai_companion",
            "name": name,
            "action_state": action_state,
            "is_current": is_current,
            "turn_position": integer(order_item.get("position"), 0),
            "participation_state": actor_status,
            "identity_facets": _identity_facets(actor_view, ui_profile),
            "attributes": attributes,
            "statuses": statuses,
            "resources": resources,
            "inventory": inventory,
            "capabilities": capabilities,
            "detail_sections": _detail_sections(
                ui_profile=ui_profile,
                attribute_view=attribute_view,
                resources=resources,
                statuses=statuses,
                inventory=inventory,
                capabilities=capability_items,
            ),
            "updated_at": latest_timestamp(
                raw.get("updated_at"), inventory.get("updated_at")
            ),
        }
        if attribute_view:
            actor["attribute_view"] = attribute_view
        items.append(actor)

    items.sort(
        key=lambda item: (
            0 if item.get("is_current") else 1,
            integer(item.get("turn_position"), 999) or 999,
            str(item.get("name")),
        )
    )
    safe_total = len(items)
    limit = max(1, min(20, int(limit)))
    visible_items = items[:limit]
    deduped_problems: list[dict[str, Any]] = []
    seen_codes: set[str] = set()
    for problem in problems:
        code = str(problem.get("code") or "")
        if code and code not in seen_codes:
            seen_codes.add(code)
            deduped_problems.append(problem)
    return {
        "items": visible_items,
        "total_items": safe_total,
        "truncated": safe_total > len(visible_items),
        "current_key": next(
            (str(item["key"]) for item in visible_items if item.get("is_current")),
            "",
        ),
        "problems": deduped_problems,
    }


__all__ = ["project_party"]
