from __future__ import annotations

from .world_time import *



def _normalize_actor_candidate_contracts(value: Any) -> Any:
    """Deep-copy actor data while normalizing every declared constraint."""

    if isinstance(value, Mapping):
        result = {
            str(key): _normalize_actor_candidate_contracts(item)
            for key, item in value.items()
        }
        if "constraints" in value:
            result["constraints"] = normalize_candidate_constraints(
                value.get("constraints")
            )
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_normalize_actor_candidate_contracts(item) for item in value]
    return value


def _row_value(row: Any, key: str) -> Any:
    """Read a column from a mapping or sqlite3.Row without raising."""

    if row is None:
        return None
    if isinstance(row, Mapping):
        return row.get(key)
    keys = getattr(row, "keys", None)
    if callable(keys):
        try:
            return row[key] if key in keys() else None
        except (IndexError, TypeError, KeyError):
            return None
    return None


def _raw_staged_creation(raw: Mapping[str, Any]) -> bool:
    """Whether the raw actor declaration enables staged card creation."""

    flow = raw.get("creation_flow")
    if isinstance(flow, Mapping) and flow.get("stages"):
        return True
    creation_stages = raw.get("creation_stages")
    if isinstance(creation_stages, Mapping) and creation_stages.get("stages"):
        return True
    fields = raw.get("fields")
    return (
        isinstance(fields, Sequence)
        and not isinstance(fields, (str, bytes))
        and any(
            isinstance(item, Mapping) and "stage" in item
            for item in fields
        )
    )


def staged_creation(template: Mapping[str, Any]) -> bool:
    """Whether a normalized card template declares staged creation.

    A template is staged when ``creation_flow.stages`` is declared or when
    at least one field carries an explicit ``stage``.  Legacy templates with
    neither are treated as single-stage (everything is A / required before
    opening).
    """

    flow = template.get("creation_flow")
    if isinstance(flow, Mapping) and flow.get("stages"):
        return True
    creation_stages = template.get("creation_stages")
    if isinstance(creation_stages, Mapping) and creation_stages.get("stages"):
        return True
    fields = template.get("fields")
    return (
        isinstance(fields, Sequence)
        and not isinstance(fields, (str, bytes))
        and any(
            isinstance(item, Mapping) and "stage" in item
            for item in fields
        )
    )


def field_stage(field: Mapping[str, Any]) -> str:
    """Return the normalized A/B/C stage of one field definition.

    Fields without an explicit ``stage`` are treated as A (opening-required).
    Invalid declared values are rejected instead of silently coerced.
    """

    raw = str(field.get("stage") or "").strip().upper()
    if not raw:
        return CARD_STAGE_A
    if raw not in CARD_STAGES:
        raise ValueError(
            f"角色字段 {field.get('key') or '?'} 声明了无效建卡阶段："
            f"{raw}（仅允许 A/B/C）"
        )
    return raw


def stage_field_projection(
    template: Mapping[str, Any],
    fields: Mapping[str, Any] | None,
    *,
    stages: Sequence[str],
) -> dict[str, Any]:
    """Project a card profile to the fields owned by selected creation stages.

    The preset snapshot registry is nested under ``_preset_refs``.  Copying that
    mapping wholesale would silently reintroduce B/C selections into an A-only
    validation pass, so both field values and their snapshots are filtered by
    the same authoritative stage declaration.
    """

    fields = fields if isinstance(fields, Mapping) else {}
    allowed_stages = {
        str(stage or "").strip().upper()
        for stage in stages
        if str(stage or "").strip()
    }
    invalid = allowed_stages.difference(CARD_STAGES)
    if invalid:
        raise ValueError(
            "角色字段投影包含无效建卡阶段："
            + "、".join(sorted(invalid))
        )
    allowed_keys = {
        str(item.get("key") or "")
        for item in template.get("fields") or []
        if isinstance(item, Mapping)
        and field_stage(item) in allowed_stages
    }
    projected = {
        key: value
        for key, value in fields.items()
        if str(key) in allowed_keys
    }
    refs = fields.get("_preset_refs")
    if isinstance(refs, Mapping):
        projected["_preset_refs"] = {
            str(key): value
            for key, value in refs.items()
            if str(key) in allowed_keys
        }
    locked = fields.get(STAGED_FIELDS_KEY)
    if isinstance(locked, (list, tuple)):
        projected[STAGED_FIELDS_KEY] = [
            str(key) for key in locked if str(key) in allowed_keys
        ]
    return projected


def stage_label(template: Mapping[str, Any], stage: str) -> str:
    """Player-facing label for one card stage."""

    stage = str(stage or "").upper()
    flow = template.get("creation_flow")
    stages = flow.get("stages") if isinstance(flow, Mapping) else {}
    if isinstance(stages, Mapping):
        entry = stages.get(stage)
        if isinstance(entry, Mapping) and entry.get("label"):
            return str(entry["label"])
        if isinstance(entry, str) and entry.strip():
            return str(entry)
    return {
        CARD_STAGE_A: "开演所需",
        CARD_STAGE_B: "第一幕补充",
        CARD_STAGE_C: "长期补充",
    }.get(stage, "角色补充")


def _field_filled(
    fields: Mapping[str, Any],
    definition: Mapping[str, Any],
) -> bool:
    key = str(definition.get("key") or "")
    value = fields.get(key)
    if isinstance(value, (list, tuple)):
        minimum = max(
            0,
            int(
                definition.get(
                    "min_choices",
                    1 if definition.get("required") else 0,
                )
                or 0
            ),
        )
        return len(value) >= minimum
    return bool(str(value or "").strip())


def stage_required_missing(
    template: Mapping[str, Any],
    fields: Mapping[str, Any] | None,
    *,
    stages: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Required visible field definitions that are still missing.

    ``stages=None`` derives the phase contract: staged worlds only require
    the A group (B/C fields never block review or opening), legacy templates
    require every required field.  An explicit ``stages`` tuple overrides.
    """

    fields = fields if isinstance(fields, Mapping) else {}
    if stages is None:
        stages = (
            (CARD_STAGE_A,)
            if staged_creation(template)
            else None
        )
    allowed = set(str(item) for item in stages) if stages is not None else None
    missing: list[dict[str, Any]] = []
    for item in template.get("fields") or []:
        if not isinstance(item, Mapping):
            continue
        if str(item.get("type") or "") == "derived":
            continue
        if allowed is not None and field_stage(item) not in allowed:
            continue
        key = str(item.get("key") or "")
        if not item.get("required"):
            continue
        if not field_visible(item, fields):
            continue
        if not _field_filled(fields, item):
            missing.append(item)
    return missing


def stage_lock_field(fields: dict[str, Any], key: str) -> None:
    """Mark one B/C field key as confirmed by the story (stage_locked)."""

    locked = fields.get(STAGED_FIELDS_KEY)
    locked = (
        [str(item) for item in locked]
        if isinstance(locked, list)
        else []
    )
    key = str(key or "")
    if key and key not in locked:
        locked.append(key)
    fields[STAGED_FIELDS_KEY] = locked


def card_stage_state(
    template: Mapping[str, Any],
    fields: Mapping[str, Any] | None,
    *,
    locked_keys: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Derive the D1 card stage state from template + draft/profile values.

    States (16_STAGED_CHARACTER_CREATION §6):
    - ``incomplete``: A 组未完成，尚不可开演；
    - ``core_ready``: A 组完成，可开演；
    - ``staged_pending``: 存在 B/C 组待补充；
    - ``stage_locked``: 已有 B/C 字段由剧情确认；
    - ``complete``: 所有必需阶段完成。
    """

    fields = fields if isinstance(fields, Mapping) else {}
    if not staged_creation(template):
        missing_all = stage_required_missing(template, fields)
        complete = not missing_all
        return {
            "stage": "complete" if complete else "incomplete",
            "core_ready": complete,
            "staged_pending": False,
            "stage_locked": False,
            "complete": complete,
            "pending_count": 0,
            "pending_fields": [],
            "pending_stage_counts": {},
            "locked_fields": [],
            "missing_a": [
                str(item.get("label") or item.get("key") or "")
                for item in missing_all
            ],
            "missing_a_count": len(missing_all),
        }
    missing_a = stage_required_missing(
        template, fields, stages=(CARD_STAGE_A,)
    )
    core_ready = not missing_a
    pending = stage_required_missing(
        template, fields, stages=(CARD_STAGE_B, CARD_STAGE_C)
    )
    pending_fields = [
        str(item.get("key") or "") for item in pending
    ]
    complete = core_ready and not pending_fields
    if locked_keys is None:
        raw_locked = fields.get(STAGED_FIELDS_KEY)
        locked_keys = (
            [str(item) for item in raw_locked]
            if isinstance(raw_locked, (list, tuple))
            else []
        )
    stage_field_keys = {
        str(item.get("key") or "")
        for item in template.get("fields") or []
        if isinstance(item, Mapping)
        and field_stage(item) in {CARD_STAGE_B, CARD_STAGE_C}
    }
    locked_fields = [
        key for key in locked_keys if key in stage_field_keys
    ]
    pending_counts = {
        stage: len(
            stage_required_missing(template, fields, stages=(stage,))
        )
        for stage in (CARD_STAGE_B, CARD_STAGE_C)
    }
    if not core_ready:
        stage = "incomplete"
    elif complete:
        stage = "complete"
    elif locked_fields:
        stage = "stage_locked"
    else:
        stage = "staged_pending"
    return {
        "stage": stage,
        "core_ready": core_ready,
        "staged_pending": core_ready and not complete,
        "stage_locked": bool(locked_fields),
        "complete": complete,
        "pending_count": len(pending_fields),
        "pending_fields": pending_fields,
        "pending_stage_counts": pending_counts,
        "locked_fields": locked_fields,
        "missing_a": [
            str(item.get("label") or item.get("key") or "")
            for item in missing_a
        ],
        "missing_a_count": len(missing_a),
    }


def resolve_card_stage(
    template: Mapping[str, Any],
    fields: Mapping[str, Any] | None,
    *,
    row: Any = None,
    locked_keys: Sequence[str] | None = None,
) -> str:
    """Backward-injectable stage resolver.

    When the ``participants`` row already carries a persisted ``card_stage``
    column (schema 20), that value is authoritative.  Until the column is
    merged, the stage is derived from the template and the card profile, so
    callers never depend on a specific schema revision.
    """

    persisted = _row_value(row, "card_stage")
    if persisted in CARD_STAGE_STATES or persisted == "incomplete":
        return str(persisted)
    return card_stage_state(
        template, fields, locked_keys=locked_keys
    )["stage"]


def card_template(world: Mapping[str, Any]) -> dict[str, Any]:
    rules = world.get("rules")
    rules = rules if isinstance(rules, Mapping) else {}
    # C3：TWP actor 模块是角色卡唯一权威内容源。
    raw_source = rules.get("actor")
    raw_source = raw_source if isinstance(raw_source, Mapping) else {}
    raw = _normalize_actor_candidate_contracts(
        _localized_actor_source(world, raw_source)
    )
    fields_raw = raw.get("fields")
    fields: list[dict[str, Any]] = []
    if isinstance(fields_raw, Sequence) and not isinstance(
        fields_raw, (str, bytes)
    ):
        seen: set[str] = set()
        # 33 字段模板需要超过30个字段。
        for item in fields_raw[:60]:
            if not isinstance(item, Mapping):
                continue
            key = re.sub(
                r"[^a-zA-Z0-9_-]",
                "",
                str(item.get("key") or "").strip(),
            )[:40]
            if not key or key in seen:
                continue
            seen.add(key)
            field_type = str(item.get("type") or "text").lower()
            if field_type not in SUPPORTED_CARD_FIELD_TYPES:
                raise ValueError(
                    f"角色字段 {key} 使用了未注册类型：{field_type}"
                )
            fields.append(
                {
                    "key": key,
                    "label": clean_text(
                        item.get("label") or key,
                        max_chars=4000,
                    ),
                    "required": bool(item.get("required", True)),
                    "private": bool(item.get("private", False)),
                    "max_chars": _bounded_int(
                        item.get("max_chars"),
                        500,
                        10,
                        4000,
                    ),
                    "type": field_type,
                    **({"text_id": str(item.get("text_id"))}
                       if item.get("text_id") else {}),
                    **({"semantic_role": str(item.get("semantic_role"))}
                       if item.get("semantic_role") else {}),
                    **({"presentation": dict(item.get("presentation") or {})}
                       if isinstance(item.get("presentation"), Mapping) else {}),
                    **({"visibility": str(item.get("visibility"))}
                       if item.get("visibility") else {}),
                    **({"summary": str(item.get("summary"))}
                       if item.get("summary") else {}),
                    **({"description": str(item.get("description"))}
                       if item.get("description") else {}),
                    **({"help": str(item.get("help"))}
                       if item.get("help") else {}),
                    **({"options": list(item.get("options") or [])}
                       if item.get("options") else {}),
                    **({"options_source": str(item.get("options_source"))}
                       if item.get("options_source") else {}),
                    **({"value_field": str(item.get("value_field"))}
                       if item.get("value_field") else {}),
                    **({"label_field": str(item.get("label_field"))}
                       if item.get("label_field") else {}),
                    **(
                        {
                            "preset_source": str(
                                item.get("preset_source")
                                or item.get("preset_set")
                            )
                        }
                        if item.get("preset_source") or item.get("preset_set")
                        else {}
                    ),
                    **({"filter_by": str(item.get("filter_by"))}
                       if item.get("filter_by") else {}),
                    **({"option_key": str(item.get("option_key"))}
                       if item.get("option_key") else {}),
                    **({"description_field": str(item.get("description_field"))}
                       if item.get("description_field") else {}),
                    **({"page_size": _bounded_int(
                        item.get("page_size"), 5, 1, 10
                    )} if item.get("page_size") is not None else {}),
                    **({"visible_when": dict(item.get("visible_when") or {})}
                       if isinstance(item.get("visible_when"), Mapping) else {}),
                    **({"constraints": normalize_candidate_constraints(
                        item.get("constraints")
                    )} if "constraints" in item else {}),
                    **({"clear_on_change": list(item.get("clear_on_change") or [])}
                       if isinstance(item.get("clear_on_change"), Sequence)
                       and not isinstance(item.get("clear_on_change"), (str, bytes))
                       else {}),
                    **({"must_differ_from": str(item.get("must_differ_from"))}
                       if item.get("must_differ_from") else {}),
                    **({"min_choices": _bounded_int(
                        item.get("min_choices"), 0, 0, 100
                    )} if item.get("min_choices") is not None else {}),
                    **({"max_choices": _bounded_int(
                        item.get("max_choices"), 1, 1, 100
                    )} if item.get("max_choices") is not None else {}),
                    **({"display_order": _bounded_int(
                        item.get("display_order"), 1000, -100000, 100000
                    )} if item.get("display_order") is not None else {}),
                    **({"stage": field_stage(item)} if "stage" in item else {}),
                }
            )
    if not fields:
        actor_enabled = any(
            isinstance(item, Mapping)
            and str(item.get("module_id") or item.get("id") or "") == "actor"
            and bool(item.get("enabled", True))
            for item in world.get("twp_modules") or []
        )
        if actor_enabled:
            raise ValueError("启用 actor 模块时必须声明至少一个角色字段")
    dimensions = normalize_preset_dimensions(raw)
    if dimensions:
        generated = dimension_fields(raw)
        generated_by_key = {str(item["key"]): item for item in generated}
        merged: list[dict[str, Any]] = []
        consumed: set[str] = set()
        for item in fields:
            key = str(item.get("key") or "")
            if key in generated_by_key:
                merged.append({**item, **generated_by_key[key]})
                consumed.add(key)
            else:
                merged.append(item)
        pending = [item for item in generated if str(item["key"]) not in consumed]
        if pending:
            insert_at = next(
                (index + 1 for index, item in enumerate(merged)
                 if str(item.get("key") or "") == "code"),
                0,
            )
            merged[insert_at:insert_at] = pending
        fields = merged
    for field in fields:
        if str(field.get("semantic_role") or "") in {
            "actor.identity.name",
            "actor.identity.alias",
        }:
            field["max_chars"] = min(
                12,
                int(field.get("max_chars", 12) or 12),
            )
    char_limit = _bounded_int(raw.get("field_char_limit"), 0, 0, 4000)
    if char_limit:
        for field in fields:
            key = str(field.get("key") or "")
            if str(field.get("semantic_role") or "") in {
                "actor.identity.name",
                "actor.identity.alias",
            }:
                continue
            if str(field.get("type") or "") == "integer":
                continue
            field["max_chars"] = min(
                int(field.get("max_chars", 4000) or 4000),
                char_limit,
            )
    stats_raw = raw.get("stats")
    stats_raw = stats_raw if isinstance(stats_raw, Mapping) else {}
    generation_raw = raw.get("stat_generation")
    if not isinstance(generation_raw, Mapping):
        nested_generation = stats_raw.get("stat_generation")
        generation_raw = (
            nested_generation
            if isinstance(nested_generation, Mapping)
            else {}
        )
    mode = (
        "preset_stack"
        if str(generation_raw.get("mode") or "").lower()
        == "preset_stack"
        else stats_mode(stats_raw)
    )
    attributes_raw = stats_raw.get("attributes")
    attributes: list[dict[str, Any]] = []
    if isinstance(attributes_raw, Sequence) and not isinstance(
        attributes_raw,
        (str, bytes),
    ):
        for item in attributes_raw[:20]:
            if not isinstance(item, Mapping):
                continue
            key = re.sub(
                r"[^a-zA-Z0-9_-]",
                "",
                str(item.get("key") or "").strip(),
            )[:40]
            if not key or any(entry["key"] == key for entry in attributes):
                continue
            minimum = _bounded_int(item.get("minimum"), 0, -100, 100)
            maximum = _bounded_int(
                item.get("maximum"),
                5,
                minimum,
                100,
            )
            attributes.append(
                {
                    "key": key,
                    "label": clean_text(
                        item.get("label") or key,
                        max_chars=4000,
                    ),
                    "minimum": minimum,
                    "maximum": maximum,
                    "default": _bounded_int(
                        item.get("default"),
                        minimum,
                        minimum,
                        maximum,
                    ),
                }
            )
    if not attributes and mode != "none":
        raise ValueError("世界启用属性生成时必须显式声明 stats.attributes")
    table_raw = stats_raw.get("modifier_table")
    table_raw = table_raw if isinstance(table_raw, Mapping) else {}
    modifier_table: dict[str, int] = {}
    for raw_value, raw_modifier in table_raw.items():
        try:
            value_key = str(int(raw_value))
            modifier = int(raw_modifier)
        except (TypeError, ValueError):
            continue
        modifier_table[value_key] = max(-10, min(10, modifier))
    budget = _bounded_int(
        stats_raw.get("budget"),
        0,
        0,
        2000,
    )
    profession_mode = mode == "preset"
    # Profession-preset mode: keep the 10 attributes for checks/preview but do
    # NOT generate 10 manual stat-entry questions (doc §4.2).
    for attribute in (attributes if mode == "manual" else []):
        field_key = f"stat_{attribute['key']}"
        existing_field = next(
            (
                item
                for item in fields
                if str(item.get("key") or "") == field_key
            ),
            None,
        )
        if existing_field is not None:
            fields.remove(existing_field)
        fields.append(
            {
                "key": field_key,
                "label": clean_text(
                    (
                        existing_field.get("label")
                        if existing_field is not None
                        else ""
                    )
                    or (
                        f"{attribute['label']}数值"
                        f"（{attribute['minimum']}—{attribute['maximum']}，"
                        f"总预算 {budget}）"
                    ),
                    max_chars=4000,
                ),
                "required": True,
                "private": False,
                "max_chars": 12,
                "type": "integer",
                "minimum": attribute["minimum"],
                "maximum": attribute["maximum"],
                "default": attribute["default"],
                "stage": CARD_STAGE_A,
                "stat_key": attribute["key"],
            }
        )
    # D1：分阶段建卡强制契约。声明分阶段的世界包，每个字段必须显式属于
    # A/B/C；维度派生字段与数值字段默认视为开演所需（A 组）。
    if _raw_staged_creation(raw):
        for field in fields:
            if "stage" in field:
                field_stage(field)
                continue
            if field.get("preset_dimension"):
                field["stage"] = CARD_STAGE_A
                continue
            raise ValueError(
                f"角色字段 {field.get('key') or '?'} 未声明建卡阶段"
                "（A/B/C），分阶段世界必须为每个字段显式归组"
            )
    preset_sets_raw = raw.get("preset_sets")
    preset_sets = (
        {
            str(key): list(value)
            for key, value in preset_sets_raw.items()
            if isinstance(value, Sequence)
            and not isinstance(value, (str, bytes))
        }
        if isinstance(preset_sets_raw, Mapping)
        else {}
    )
    profession_presets = list(raw.get("profession_presets") or [])
    origin_region_presets = list(raw.get("origin_region_presets") or [])
    social_identity_presets = list(raw.get("social_identity_presets") or [])
    if profession_presets:
        preset_sets.setdefault("profession_presets", profession_presets)
    if origin_region_presets:
        preset_sets.setdefault("origin_region_presets", origin_region_presets)
    if social_identity_presets:
        preset_sets.setdefault("social_identity_presets", social_identity_presets)
    profession_field = next(
        (
            item for item in fields
            if str(item.get("semantic_role") or "")
            == "actor.identity.profession"
        ),
        None,
    )
    if not profession_presets and isinstance(profession_field, Mapping):
        profession_source = str(
            profession_field.get("preset_source")
            or profession_field.get("preset_set")
            or profession_field.get("options_source")
            or ""
        )
        if profession_source in preset_sets:
            profession_presets = list(preset_sets[profession_source])
    normalized_generation = {
        "mode": str(generation_raw.get("mode") or mode).lower(),
        "base_stats": dict(generation_raw.get("base_stats") or {}),
        "bonus_sources": list(
            generation_raw.get("bonus_sources") or []
        ),
        "bonus_source_rules": dict(
            generation_raw.get("bonus_source_rules") or {}
        ),
        "expected_total": generation_raw.get(
            "expected_total", budget
        ),
        "min_per_stat": generation_raw.get("min_per_stat"),
        "max_per_stat": generation_raw.get("max_per_stat"),
        "allow_manual_edit": bool(
            generation_raw.get("allow_manual_edit", False)
        ),
    }
    return {
        "version": _bounded_int(raw.get("version"), 1, 1, 100000),
        "candidate_contract": str(
            raw.get("candidate_contract")
            or "twp-actor-candidate/1.0.0-rc10"
        ),
        "auto_approve": bool(raw.get("auto_approve", False)),
        "edit_requires_review": bool(
            raw.get("edit_requires_review", True)
        ),
        "fields": fields,
        "stats": {
            "mode": mode,
            "budget": budget,
            "attributes": attributes,
            "modifier_table": modifier_table,
            "input_mode": str(stats_raw.get("input_mode") or ""),
            "allocation_mode": str(
                stats_raw.get("allocation_mode") or ""
            ),
            "primary_bonus": _bounded_int(
                stats_raw.get("primary_bonus"), 7, 0, 100
            ),
            "secondary_bonus": _bounded_int(
                stats_raw.get("secondary_bonus"), 3, 0, 100
            ),
            "allocation": dict(stats_raw.get("allocation") or {}),
            "total_validation": dict(stats_raw.get("total_validation") or {}),
            "preset_selector": dict(stats_raw.get("preset_selector") or {}),
            "bonus_choices": list(stats_raw.get("bonus_choices") or []),
            "stat_generation": dict(normalized_generation),
        },
        # Keep the canonical declaration available to editor/API clients.
        "stat_generation": dict(normalized_generation),
        "profession_presets": profession_presets,
        "origin_region_presets": origin_region_presets,
        "social_identity_presets": social_identity_presets,
        "preset_sets": preset_sets,
        "preset_dimensions": dimensions,
        "knowledge_profiles": dict(raw.get("knowledge_profiles") or {}),
        "content_profiles": dict(raw.get("content_profiles") or {}),
        "creation_flow": _json_copy(raw.get("creation_flow")),
        "creation_stages": _json_copy(raw.get("creation_stages")),
        "profession_mode": profession_mode,
    }


def card_stat_allocation(
    template: Mapping[str, Any],
    fields: Mapping[str, Any] | None = None,
    current_step: int | None = None,
) -> dict[str, Any]:
    """Return authoritative progress for none, manual, or preset stats."""

    # ``validation`` imports this module to reuse the normalized card helpers,
    # so importing these two functions at module load time would create a
    # cycle.  Resolve them only when allocation is evaluated; by then both
    # lifecycle modules have finished loading.  Without this deferred import,
    # profession-preset worlds raise NameError while rendering every ordinary
    # text step (including mode selection replies and ``/团 当前``).
    from .validation import (
        resolve_profession_stats,
        uses_profession_preset_stats,
    )

    stats_config = template.get("stats") or {}
    mode = stats_mode(stats_config)
    if mode == "none":
        return {"mode": "none", "stat_fields": [], "current": None, "values": {}, "used": 0, "budget": 0, "remaining": 0, "complete": True}
    if uses_preset_stack_stats(template):
        safe_fields = fields if isinstance(fields, Mapping) else {}
        resolved = calculate_preset_stack_stats(
            template,
            safe_fields,
            require_complete=False,
        )
        config = stat_generation_config(template)
        budget = int(config.get("expected_total") or 0)
        return {
            "mode": "preset_stack",
            "stat_fields": [],
            "current": None,
            "values": resolved["raw"] if resolved else {},
            "base_values": resolved["base"] if resolved else dict(config.get("base_stats") or {}),
            "used": resolved["effective_total"] if resolved else 0,
            "budget": budget,
            "remaining": 0 if resolved else budget,
            "complete": resolved is not None,
            "resolved": resolved,
        }
    if uses_profession_preset_stats(template):
        # Profession-preset mode: stats are derived, never manually allocated.
        safe_fields = fields if isinstance(fields, Mapping) else {}
        try:
            resolved = resolve_profession_stats(
                template, safe_fields, require_complete=False
            )
        except ValueError:
            resolved = None
        total_validation = stats_config.get("total_validation") or {}
        final_total = int(total_validation.get("final_total", stats_config.get("budget", 0)))
        return {
            "mode": "preset",
            "stat_fields": [],
            "current": None,
            "values": resolved["raw"] if resolved else {},
            "base_values": resolved["base"] if resolved else {},
            "used": resolved["effective_total"] if resolved else 0,
            "budget": final_total,
            "remaining": (
                max(0, final_total - resolved["effective_total"])
                if resolved else final_total
            ),
            "resolved": resolved,
        }

    field_values = fields if isinstance(fields, Mapping) else {}
    definitions = template.get("fields")
    definitions = (
        list(definitions)
        if isinstance(definitions, Sequence)
        and not isinstance(definitions, (str, bytes))
        else []
    )
    stats = template.get("stats")
    stats = stats if isinstance(stats, Mapping) else {}
    attributes_raw = stats.get("attributes")
    attributes = (
        list(attributes_raw)
        if isinstance(attributes_raw, Sequence)
        and not isinstance(attributes_raw, (str, bytes))
        else []
    )
    attributes_by_key = {
        str(item.get("key") or ""): item
        for item in attributes
        if isinstance(item, Mapping) and str(item.get("key") or "")
    }
    stat_fields: list[dict[str, Any]] = []
    for index, definition in enumerate(definitions):
        if not isinstance(definition, Mapping):
            continue
        stat_key = str(definition.get("stat_key") or "")
        attribute = attributes_by_key.get(stat_key)
        if not stat_key or not isinstance(attribute, Mapping):
            continue
        stat_fields.append(
            {
                "step": index,
                "field_key": str(definition.get("key") or f"stat_{stat_key}"),
                "stat_key": stat_key,
                "label": str(attribute.get("label") or stat_key),
                "description": str(attribute.get("description") or ""),
                "minimum": int(attribute.get("minimum", 0)),
                "maximum": int(attribute.get("maximum", 0)),
                "default": int(attribute.get("default", 0)),
            }
        )

    values: dict[str, int] = {}
    for item in stat_fields:
        field_key = item["field_key"]
        if field_key not in field_values:
            continue
        try:
            values[field_key] = int(field_values[field_key])
        except (TypeError, ValueError):
            continue

    budget = int(stats.get("budget", 0) or 0)
    used = sum(values.values())
    allocation = stats.get("allocation") or {}
    rule = str(allocation.get("rule") or "maximum")
    target = int(allocation.get("total", budget))
    total_ok = (True if rule == "none" else used <= target if rule == "maximum" else used == target if rule == "exact" else int(allocation.get("minimum_total", 0)) <= used <= int(allocation.get("maximum_total", budget)))
    result: dict[str, Any] = {
        "mode": "manual",
        "budget": budget,
        "used": used,
        "remaining": budget - used,
        "values": values,
        "stat_fields": stat_fields,
        "first_step": stat_fields[0]["step"] if stat_fields else len(definitions),
        "complete": bool(stat_fields)
        and all(item["field_key"] in values for item in stat_fields)
        and total_ok,
        "total_ok": total_ok,
        "allocation_rule": rule,
        "current": None,
    }

    if current_step is None:
        return result
    current = next(
        (item for item in stat_fields if item["step"] == int(current_step)),
        None,
    )
    if not current:
        return result

    current_value = values.get(current["field_key"])
    used_before = used - (current_value if current_value is not None else 0)
    reserved_minimum = sum(
        item["minimum"]
        for item in stat_fields
        if item["step"] > current["step"]
        and item["field_key"] not in values
    )
    effective_maximum = min(
        current["maximum"],
        budget - used_before - reserved_minimum,
    )
    current_position = next(
        index
        for index, item in enumerate(stat_fields, start=1)
        if item["step"] == current["step"]
    )
    result["current"] = {
        **current,
        "position": current_position,
        "total": len(stat_fields),
        "used_before": used_before,
        "remaining_before": budget - used_before,
        "reserved_minimum": reserved_minimum,
        "effective_maximum": effective_maximum,
    }
    return result


PROFESSION_PRESET_STAT_MODE = (
    "automatic_profession_base_plus_two_fixed_bonus_choices"
)



__all__ = [name for name in globals() if not name.startswith('__')]
