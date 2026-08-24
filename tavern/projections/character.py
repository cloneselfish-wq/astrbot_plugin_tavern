from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from ..field_accounting import FieldAccountingError, field_account


VIEWER_ROLES = frozenset({"player", "character", "dm", "admin", "author", "remote"})
PRIVATE_VIEWERS = frozenset({"character", "dm", "admin", "author"})

GROUP_LABELS = {
    "identity": "身份信息",
    "background": "背景与立场",
    "equipment": "装备与携行",
    "story": "故事与关系",
    "capabilities": "能力与代价",
    "custom": "其他信息",
}

ROLE_ICONS = {
    "actor.identity.name": "identity.name",
    "actor.identity.alias": "identity.name",
    "actor.identity.species": "identity.species",
    "actor.identity.profession": "identity.profession",
    "actor.capability.list": "capability",
    "actor.weakness.list": "capability",
    "actor.goal.primary": "story",
    "actor.secret.private": "story",
}

RESOURCE_CAPABILITIES = {
    "inventory": frozenset({"inventory.personal.read"}),
    "economy": frozenset({"economy.wallet.read"}),
    "quests": frozenset({"quest.read"}),
    "story": frozenset({"scene.read"}),
}

STATE_LABELS: dict[str, dict[str, tuple[str, str]]] = {
    "quest_graph": {
        "available": ("可接取", "线索已经出现，但队伍尚未正式接受任务。"),
        "inactive": ("尚未激活", "任务条件尚未满足。"),
        "active": ("进行中", "任务已经接受并正在推进。"),
        "blocked": ("受阻", "任务暂时无法继续推进。"),
        "completed": ("已完成", "任务目标已经完成。"),
        "failed": ("已失败", "任务已经失败。"),
        "abandoned": ("已放弃", "队伍已经放弃该任务。"),
    },
    "faction_state": {
        "neutral": ("中立", "尚未建立稳定合作或敌对关系。"),
        "friendly": ("友好", "该组织目前对队伍持友好态度。"),
        "allied": ("同盟", "该组织目前与队伍保持同盟关系。"),
        "wary": ("戒备", "该组织正在谨慎观察队伍。"),
        "hostile": ("敌对", "该组织目前对队伍持敌对态度。"),
    },
    "npc_presence": {
        "active": ("在场", "角色仍在当前故事中活动。"),
        "departed": ("离场", "角色已经离开当前故事现场。"),
        "missing": ("失踪", "角色当前下落不明。"),
        "dead": ("死亡", "角色已经死亡。"),
        "archived": ("归档", "角色已退出当前故事。"),
    },
    "npc_condition": {
        "normal": ("正常", "角色当前没有需要特别说明的状况。"),
        "wounded": ("受伤", "角色当前处于受伤状态。"),
        "unconscious": ("昏迷", "角色当前失去意识。"),
        "detained": ("被拘押", "角色当前受到拘押。"),
        "cursed": ("受诅咒", "角色当前受到诅咒影响。"),
        "hostile": ("敌对", "角色当前对队伍抱有敌意。"),
    },
}


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def actor_definition(world: Mapping[str, Any]) -> dict[str, Any]:
    rules = _mapping(world.get("rules"))
    return _mapping(rules.get("actor"))


def resolved_catalog(
    world: Mapping[str, Any],
    locale: str | None = None,
) -> tuple[dict[str, str], str, bool]:
    """Return one frozen catalog and whether locale fallback was required."""

    catalogs = _mapping(world.get("resolved_text_catalog"))
    metadata = _mapping(world.get("localization_metadata"))
    rules = _mapping(world.get("rules"))
    config = _mapping(rules.get("localization"))
    default_locale = str(
        metadata.get("default_locale")
        or config.get("default_locale")
        or "zh-CN"
    )
    requested = str(locale or default_locale).strip() or default_locale
    selected = requested if isinstance(catalogs.get(requested), Mapping) else default_locale
    catalog = _mapping(catalogs.get(selected))
    return (
        {str(key): str(value) for key, value in catalog.items()},
        selected,
        selected != requested,
    )


def _text(
    catalog: Mapping[str, str],
    key: str,
    problems: list[dict[str, Any]],
) -> str:
    value = str(catalog.get(key) or "").strip()
    if not value:
        problems.append(
            {
                "code": "projection.text_missing",
                "path": key,
                "message": "冻结文本目录中缺少投影文本",
            }
        )
    return value


def semantic_field_index(world: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for field in _sequence(actor_definition(world).get("fields")):
        if not isinstance(field, Mapping):
            continue
        role = str(field.get("semantic_role") or "").strip()
        if role:
            if role in result:
                raise ValueError(f"角色字段重复声明语义角色：{role}")
            result[role] = dict(field)
    return result


def field_for_role(
    world: Mapping[str, Any],
    semantic_role: str,
) -> dict[str, Any] | None:
    return semantic_field_index(world).get(str(semantic_role or "").strip())


def _preset_snapshot(profile: Mapping[str, Any], field_id: str) -> Any:
    refs = profile.get("_preset_refs")
    if not isinstance(refs, Mapping):
        return None
    return refs.get(field_id)


def _preset_index(
    world: Mapping[str, Any],
    field: Mapping[str, Any],
    profile: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Return a field-aware option index, including dependent nested choices."""

    template = actor_definition(world)
    options: list[dict[str, Any]] = []
    try:
        # Imported lazily so the projection module stays usable during lifecycle
        # bootstrap.  This is the same resolver used by the card wizard and is
        # therefore authoritative for profession-dependent option lists.
        from ..card_wizard import preset_options

        options = preset_options(template, field, profile or {})
    except (KeyError, TypeError, ValueError):
        source = str(
            field.get("preset_source")
            or field.get("preset_set")
            or field.get("options_source")
            or ""
        ).strip()
        preset_sets = _mapping(template.get("preset_sets"))
        raw_options = preset_sets.get(source)
        if isinstance(raw_options, Mapping):
            raw_options = raw_options.get("options") or raw_options.get("items") or []
        options = [dict(item) for item in _sequence(raw_options) if isinstance(item, Mapping)]

    result: dict[str, dict[str, Any]] = {}
    for option in options:
        source = _mapping(option.get("source"))
        aliases = _sequence(option.get("aliases")) + _sequence(source.get("aliases"))
        identities = [
            option.get("id"),
            option.get("value"),
            option.get("label"),
            source.get("id"),
            source.get("value"),
            source.get("selection_value"),
            source.get("label"),
            source.get("name"),
            *aliases,
        ]
        for identity in identities:
            key = str(identity or "").strip()
            if key:
                result.setdefault(key, dict(option))
                result.setdefault(key.casefold(), dict(option))
    return result


def _entity_label_index(world: Mapping[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    raw_index = world.get("entity_index")
    if isinstance(raw_index, Mapping):
        rows = [
            {"id": key, **dict(value)} if isinstance(value, Mapping) else {"id": key, "label": value}
            for key, value in raw_index.items()
        ]
    else:
        rows = _sequence(raw_index)
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        label = str(raw.get("label") or raw.get("name") or "").strip()
        if not label:
            continue
        for value in (
            raw.get("id"),
            raw.get("short_ref"),
            raw.get("canonical_ref"),
        ):
            ref = str(value or "").strip()
            if ref:
                result.setdefault(ref, label)
                result.setdefault(ref.casefold(), label)
    return result


def _looks_internal_ref(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if ":" in text or text.startswith(("twp-", "pcard", "participant_")):
        return True
    return "_" in text and text.isascii() and " " not in text


def _friendly_mapping_text(value: Mapping[str, Any], *, identity: str = "") -> str:
    for key in ("label", "name", "display_name", "title"):
        text = str(value.get(key) or "").strip()
        if text and text != identity and not _looks_internal_ref(text):
            return text
    candidate = str(value.get("value") or "").strip()
    if candidate and candidate != identity and not _looks_internal_ref(candidate):
        return candidate
    return ""


def _display_scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Mapping):
        return _friendly_mapping_text(value)
    return str(value).strip()


def field_display_value(
    world: Mapping[str, Any],
    profile: Mapping[str, Any],
    field: Mapping[str, Any],
    *,
    catalog: Mapping[str, str],
    problems: list[dict[str, Any]],
) -> str:
    field_id = str(field.get("key") or "")
    snapshot = _preset_snapshot(profile, field_id)
    option_index = _preset_index(world, field, profile)
    entity_labels = _entity_label_index(world)
    is_preset = bool(
        field.get("preset_source")
        or field.get("preset_set")
        or field.get("options_source")
        or field.get("options")
        or snapshot is not None
    )

    def option_label(value: Any) -> str:
        option_id = ""
        if isinstance(value, Mapping):
            option_id = str(
                value.get("id")
                or value.get("ref")
                or value.get("value")
                or ""
            ).strip()
        elif isinstance(value, str):
            option_id = value.strip()
        option = option_index.get(option_id) or option_index.get(option_id.casefold())
        if option:
            source = _mapping(option.get("source"))
            root = str(
                source.get("text_id")
                or option.get("text_id")
                or (
                    "preset_sets."
                    f"{field.get('preset_source') or field.get('preset_set')}."
                    f"{option_id}"
                )
            )
            localized = str(catalog.get(f"{root}.label") or "").strip()
            if localized:
                return localized
            friendly = _friendly_mapping_text(source, identity=option_id)
            if friendly:
                return friendly
            friendly = _friendly_mapping_text(option, identity=option_id)
            if friendly:
                return friendly
        entity_label = entity_labels.get(option_id) or entity_labels.get(option_id.casefold())
        if entity_label and entity_label != option_id:
            return entity_label
        return ""

    def snapshot_label(value: Any) -> str:
        if not isinstance(value, Mapping):
            return ""
        identity = str(value.get("id") or value.get("ref") or "").strip()
        # The frozen player-facing label is the first authority.  It survives
        # candidate reordering and even removal from a newer template.
        direct = _friendly_mapping_text(value, identity=identity)
        if direct:
            return direct
        frozen = _mapping(value.get("snapshot"))
        root = str(frozen.get("text_id") or "").strip()
        localized = str(catalog.get(f"{root}.label") or "").strip() if root else ""
        if localized:
            return localized
        direct = _friendly_mapping_text(frozen, identity=identity)
        return direct or option_label(value)

    if isinstance(snapshot, Mapping):
        label = snapshot_label(snapshot)
        if label:
            return label
    if isinstance(snapshot, Sequence) and not isinstance(snapshot, (str, bytes)):
        labels = [snapshot_label(item) for item in snapshot]
        labels = [item for item in labels if item]
        if labels and len(labels) == len(snapshot):
            return "、".join(labels)
    value = profile.get(field_id)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        labels = [option_label(item) for item in value]
        if not is_preset:
            labels = [label or _display_scalar(item) for label, item in zip(labels, value)]
        labels = [item for item in labels if item]
        if labels and (not is_preset or len(labels) == len(value)):
            return "、".join(labels)
        if is_preset and value:
            problems.append(
                {
                    "code": "projection.preset_unresolved",
                    "path": f"actor.fields.{field_id}",
                    "message": "角色选项名称解析失败",
                }
            )
            return ""
    localized = option_label(value)
    if localized:
        return localized
    if is_preset and value not in (None, ""):
        problems.append(
            {
                "code": "projection.preset_unresolved",
                "path": f"actor.fields.{field_id}",
                "message": "角色选项名称解析失败",
            }
        )
        return ""
    return _display_scalar(value)


def project_actor_view(
    world: Mapping[str, Any],
    profile: Mapping[str, Any] | None,
    *,
    viewer_role: str = "player",
    include_technical_refs: bool = False,
    locale: str | None = None,
) -> dict[str, Any]:
    role = str(viewer_role or "player").strip().lower()
    if role not in VIEWER_ROLES:
        raise ValueError(f"未知角色投影视角：{viewer_role}")
    values = _mapping(profile)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    group_order: dict[str, int] = {}
    role_values: dict[str, str] = {}
    problems: list[dict[str, Any]] = []
    catalog, selected_locale, fallback_used = resolved_catalog(world, locale)
    if not catalog:
        problems.append(
            {
                "code": "projection.catalog_missing",
                "path": "resolved_text_catalog",
                "message": "世界 Artifact 缺少已冻结文本目录",
            }
        )

    for index, raw in enumerate(_sequence(actor_definition(world).get("fields"))):
        if not isinstance(raw, Mapping):
            continue
        field = dict(raw)
        field_id = str(field.get("key") or "").strip()
        if not field_id:
            continue
        private = bool(field.get("private")) or str(
            field.get("visibility") or "public"
        ).lower() in {"private", "dm", "author"}
        if private and role not in PRIVATE_VIEWERS:
            continue
        presentation = _mapping(field.get("presentation"))
        group = str(presentation.get("group") or "custom").strip() or "custom"
        order = int(presentation.get("order") or (index + 1) * 10)
        group_order[group] = min(group_order.get(group, order), order)
        problem_start = len(problems)
        display_value = field_display_value(
            world,
            values,
            field,
            catalog=catalog,
            problems=problems,
        )
        semantic_role = str(field.get("semantic_role") or "").strip()
        if semantic_role and display_value:
            role_values[semantic_role] = display_value
        item = {
            "field_id": field_id,
            "role": semantic_role,
            "label": _text(
                catalog,
                f"{str(field.get('text_id') or f'actor.fields.{field_id}')}.label",
                problems,
            ),
            "icon": str(
                presentation.get("icon")
                or ROLE_ICONS.get(semantic_role)
                or "custom"
            ),
            "display_value": display_value,
            "private": private,
            "order": order,
        }
        field_problems = problems[problem_start:]
        if any(
            str(problem.get("code") or "") == "projection.preset_unresolved"
            for problem in field_problems
        ):
            item["display_error"] = "选项名称解析失败，请让管理员检查世界包。"
        if include_technical_refs and role in {"admin", "author"}:
            item["raw_value"] = deepcopy(values.get(field_id))
            item["preset_ref"] = deepcopy(_preset_snapshot(values, field_id))
        grouped[group].append(item)

    sections = [
        {
            "id": group,
            "label": GROUP_LABELS.get(group, GROUP_LABELS["custom"]),
            "items": sorted(items, key=lambda item: (item["order"], item["field_id"])),
        }
        for group, items in sorted(
            grouped.items(),
            key=lambda item: (group_order.get(item[0], 1_000_000), item[0]),
        )
    ]
    return {
        "schema": "tavern-actor-view/1.0.0-rc10",
        "locale": selected_locale,
        "locale_fallback": fallback_used,
        "title": role_values.get("actor.identity.name", ""),
        "subtitle": role_values.get("actor.identity.alias", ""),
        "semantic_values": role_values,
        "sections": sections,
        "problems": problems,
    }

__all__ = [name for name in globals() if not name.startswith('__')]
