from __future__ import annotations

from .characters_support import *


class CharacterCardsMutationsRepositoryMixin:
    def _fill_card_draft(
        self,
        private_origin: str,
        value: str,
        source_event_id: str = "",
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT pt.*, d.id AS draft_id, d.fields_json,
                           d.current_step, d.status AS draft_status,
                           d.expires_at AS draft_expires_at,
                           ic.world_snapshot_json,
                           s.state AS session_state
                    FROM participants pt
                    JOIN character_card_drafts d
                      ON d.participant_id = pt.id
                    JOIN instance_configs ic
                      ON ic.session_id = pt.session_id
                    JOIN sessions s ON s.id = pt.session_id
                    WHERE pt.private_origin = ?
                      AND d.status IN ('active', 'suspended')
                      AND s.state <> 'finished'
                    ORDER BY d.updated_at DESC LIMIT 1
                    """,
                    (private_origin,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("当前私聊没有进行中的建卡流程")
                now = utc_now()
                if (
                    str(row["draft_status"] or "") == "suspended"
                    or str(row["session_state"] or "") == "closed"
                ):
                    raise InvalidTransitionError(
                        "继续建卡失败：当前副本已关闭。"
                        "系统已保留你的建卡资料，没有写入本次内容。"
                        "副本重新开放后请发送 /团 当前步骤"
                    )
                if row["draft_expires_at"] and row["draft_expires_at"] <= now:
                    connection.execute(
                        """
                        UPDATE character_card_drafts
                        SET status = 'expired', updated_at = ?
                        WHERE id = ?
                        """,
                        (now, row["draft_id"]),
                    )
                    raise ValueError("角色卡草稿已过期，请回群重新申请")
                world_snapshot = json_load(row["world_snapshot_json"], {})
                template = card_template(world_snapshot)
                fields_def = template["fields"]
                # D1：开演前向导只呈现 A 组；B/C 组字段留待剧情触发补充，
                # 不阻塞开演。
                allow_stages = (
                    (CARD_STAGE_A,) if staged_creation(template) else None
                )
                step = min(max(0, int(row["current_step"])), len(fields_def))
                fields = json_load(row["fields_json"], {})
                if not isinstance(fields, dict):
                    fields = {}
                source_event_id = clean_text(source_event_id, max_chars=160)
                if (
                    source_event_id
                    and str(fields.get(LAST_MESSAGE_KEY) or "")
                    == source_event_id
                ):
                    connection.execute("COMMIT")
                    return {
                        "participant_id": row["id"],
                        "session_id": row["session_id"],
                        "fields": fields,
                        "template": template,
                        "current_step": step,
                        "complete": wizard_completion_state(
                            template,
                            fields,
                            step,
                            allow_stages=allow_stages,
                        )["complete"],
                        "world": json_load(row["world_snapshot_json"], {}),
                        "duplicate": True,
                    }
                # Repair legacy drafts that still carry hand-filled stat_* fields
                # (doc §7): recompute from profession + primary/secondary and fix
                # the cursor to the first non-attribute field.
                fields, step = _repair_semantic_profession_draft(
                    template, fields, step
                )
                # B1：三种建卡模式与快速原型包（§6）。合成步骤不写入最终角色卡。
                flow = creation_flow(template)
                has_modes = bool(flow.get("modes"))
                creation_mode = current_creation_mode(fields)
                if has_modes and not creation_mode:
                    definition = mode_step(template)
                    options = preset_options(template, definition, fields)
                    try:
                        selected = choose_option(template, definition, fields, value)
                    except ValueError:
                        # B1：首条消息不是模式名时，默认进入深度模式并继续主流程，
                        # 保证旧流程/测试助手仍可直接填写姓名等字段。
                        fields["_creation_mode"] = "deep"
                        creation_mode = "deep"
                    else:
                        fields["_creation_mode"] = str(selected.get("id") or "")
                        creation_mode = str(selected.get("id") or "")
                        if creation_mode == "quick":
                            auto_fill_for_phase(
                                template,
                                fields,
                                "pre_archetype",
                            )
                        else:
                            auto_fill_for_phase(
                                template,
                                fields,
                                "post_archetype",
                            )
                        if source_event_id:
                            fields[LAST_MESSAGE_KEY] = source_event_id
                        connection.execute(
                            """
                            UPDATE character_card_drafts
                            SET fields_json = ?, current_step = 0, updated_at = ?
                            WHERE id = ?
                            """,
                            (json_dump(fields), now, row["draft_id"]),
                        )
                        connection.execute("COMMIT")
                        return {
                            "participant_id": row["id"],
                            "session_id": row["session_id"],
                            "fields": fields,
                            "template": template,
                            "current_step": 0,
                            "complete": False,
                            "world": json_load(row["world_snapshot_json"], {}),
                            "mode_chosen": True,
                            "selection_confirmation": {
                                "field_key": "_creation_mode",
                                "field_label": str(
                                    definition.get("label") or "建卡模式"
                                ),
                                "value_label": str(
                                    selected.get("label")
                                    or selected.get("value")
                                    or selected.get("id")
                                    or ""
                                ),
                                "kind": "preset",
                            },
                        }
                if has_modes and creation_mode == "quick" and not fields.get("_archetype_id"):
                    definition = archetype_step(template)
                    options = preset_options(template, definition, fields)
                    selected = choose_option(template, definition, fields, value)
                    fields["_archetype_id"] = str(selected.get("id") or "")
                    pack = next(
                        (
                            item for item in archetype_packs(template)
                            if str(item.get("id") or "") == fields["_archetype_id"]
                        ),
                        {},
                    )
                    archetype_result = apply_archetype_pack_atomic(
                        template,
                        fields,
                        pack,
                    )
                    if not archetype_result.get("ok"):
                        raise ValueError(
                            "套用角色原型失败："
                            + str(
                                archetype_result.get("reason")
                                or "原型与当前世界内容冲突。"
                            )
                            + "\n\n系统处理\n"
                            + str(
                                archetype_result.get("recovery")
                                or "系统未修改其他建卡资料。"
                            )
                            + "\n\n下一步\n请重新查看角色原型后再选择。"
                        )
                    fields = dict(archetype_result["fields"])
                    step = next_player_fillable_step(
                        template,
                        fields,
                        0,
                        allow_stages=allow_stages,
                    )
                    if source_event_id:
                        fields[LAST_MESSAGE_KEY] = source_event_id
                    connection.execute(
                        """
                        UPDATE character_card_drafts
                        SET fields_json = ?, current_step = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            json_dump(fields),
                            step,
                            now,
                            row["draft_id"],
                        ),
                    )
                    connection.execute("COMMIT")
                    return {
                        "participant_id": row["id"],
                        "session_id": row["session_id"],
                        "fields": fields,
                        "template": template,
                        "current_step": step,
                        "complete": False,
                        "world": json_load(row["world_snapshot_json"], {}),
                        "archetype_applied": True,
                        "archetype_result": {
                            key: value
                            for key, value in archetype_result.items()
                            if key != "fields"
                        },
                        "selection_confirmation": {
                            "field_key": "_archetype_id",
                            "field_label": str(
                                definition.get("label") or "角色原型"
                            ),
                            "value_label": str(
                                selected.get("label")
                                or selected.get("value")
                                or selected.get("id")
                                or ""
                            ),
                            "kind": "preset",
                        },
                    }
                if has_modes and creation_mode in {"quick", "standard"}:
                    auto_fill_for_phase(
                        template,
                        fields,
                        "resume_repair",
                    )
                if uses_preset_stack_stats(template):
                    sync_preset_stack_fields(
                        template,
                        fields,
                        require_complete=False,
                    )
                stack_was_resolved = bool(
                    fields.get(STAT_GENERATION_SNAPSHOT_KEY)
                )
                if step >= len(fields_def):
                    completion = wizard_completion_state(
                        template,
                        fields,
                        step,
                        allow_stages=allow_stages,
                    )
                    if not completion["complete"] and int(
                        completion["next_step"]
                    ) < len(fields_def):
                        step = int(completion["next_step"])
                    else:
                        raise ValueError(
                            "所有必填资料已完成，请发送 /团 确认建卡"
                        )
                definition = fields_def[step]
                options = preset_options(template, definition, fields)
                multi_presets = (
                    choose_options(template, definition, fields, value)
                    if options and definition.get("type") == "multi_select"
                    else []
                )
                selected_preset = (
                    choose_option(template, definition, fields, value)
                    if options and definition.get("type") != "multi_select"
                    else None
                )
                raw_value = (
                    selected_preset["id"]
                    if selected_preset
                    else value
                )
                if definition.get("type") == "multi_select" and options:
                    stored_value = validate_card_field(
                        definition,
                        [str(item["id"]) for item in multi_presets],
                        options,
                    )
                    text = "、".join(str(item["label"]) for item in multi_presets)
                elif selected_preset:
                    stored_value = validate_card_field(
                        definition,
                        raw_value,
                        options,
                    )
                    text = str(selected_preset.get("label") or stored_value)
                else:
                    stored_value = validate_card_field(
                        definition,
                        raw_value,
                    )
                    text = str(stored_value)
                if definition.get("type") == "integer":
                    minimum = int(definition.get("minimum", -100))
                    maximum = int(definition.get("maximum", 100))
                    allocation = card_stat_allocation(
                        template,
                        fields,
                        step,
                    )
                    current_stat = allocation.get("current")
                    if isinstance(current_stat, Mapping):
                        maximum = int(
                            current_stat["effective_maximum"]
                        )
                        if maximum < minimum:
                            raise ValueError(
                                "当前属性预算无法满足模板最低值，"
                                "请让管理员检查角色卡模板"
                            )
                    if not minimum <= stored_value <= maximum:
                        suffix = ""
                        if isinstance(current_stat, Mapping):
                            suffix = (
                                f"（总预算 {allocation['budget']}，"
                                f"已使用 {current_stat['used_before']}，"
                                f"后续至少预留 "
                                f"{current_stat['reserved_minimum']}）"
                            )
                        raise ValueError(
                            f"{definition['label']}当前必须在 "
                            f"{minimum}—{maximum} 之间{suffix}"
                        )
                profession_mode = uses_profession_preset_stats(template)
                preset_stack_mode = uses_preset_stack_stats(template)
                field_key = str(definition["key"])
                profession_key = _field_key_for_semantic_role(
                    template, _ACTOR_PROFESSION_ROLE
                )
                primary_stat_key = _field_key_for_semantic_role(
                    template, _ACTOR_PRIMARY_STAT_ROLE
                )
                secondary_stat_key = _field_key_for_semantic_role(
                    template, _ACTOR_SECONDARY_STAT_ROLE
                )
                if (profession_mode or preset_stack_mode) and field_key.startswith("stat_"):
                    raise ValueError(
                        "本世界的属性由预设自动生成，不支持手动填写。"
                    )
                previous_value = fields.get(field_key)
                if previous_value is not None and previous_value != stored_value:
                    clear_field_and_dependents(template, fields, field_key)
                different_from = str(definition.get("must_differ_from") or "")
                if different_from and fields.get(different_from) == stored_value:
                    raise ValueError(
                        f"{definition['label']}不能与"
                        f"{next((item.get('label') for item in fields_def if item.get('key') == different_from), different_from)}相同"
                    )
                fields[field_key] = stored_value
                if selected_preset:
                    store_preset_snapshot(fields, field_key, selected_preset)
                elif multi_presets:
                    store_preset_snapshots(fields, field_key, multi_presets)
                dependency_check = revalidate_dependent_selections(
                    template,
                    fields,
                )
                if template.get("preset_dimensions"):
                    validate_preset_selection(
                        template,
                        fields,
                        require_complete=False,
                    )
                stack_resolved = None
                if profession_mode and field_key == profession_key:
                    resolved = _resolve_semantic_profession_stats(
                        template, fields, require_complete=False
                    )
                    fields["profession_base_stats"] = resolved["base"]
                    for _k, _v in resolved["base"].items():
                        fields[f"stat_{_k}"] = _v
                    if primary_stat_key:
                        fields.pop(primary_stat_key, None)
                    if secondary_stat_key:
                        fields.pop(secondary_stat_key, None)
                elif profession_mode and field_key == primary_stat_key:
                    if fields.get(secondary_stat_key) == fields.get(
                        primary_stat_key
                    ):
                        fields.pop(secondary_stat_key, None)
                    resolved = _resolve_semantic_profession_stats(
                        template, fields, require_complete=False
                    )
                    for _k, _v in resolved["raw"].items():
                        fields[f"stat_{_k}"] = _v
                elif profession_mode and field_key == secondary_stat_key:
                    if fields.get(primary_stat_key) == fields.get(
                        secondary_stat_key
                    ):
                        raise ValueError("副属性不能与主属性相同")
                    resolved = _resolve_semantic_profession_stats(
                        template, fields, require_complete=True
                    )
                    for _k, _v in resolved["raw"].items():
                        fields[f"stat_{_k}"] = _v
                    fields["resolved_stat_total"] = int(resolved["effective_total"])
                if preset_stack_mode:
                    stack_resolved = sync_preset_stack_fields(
                        template,
                        fields,
                        require_complete=False,
                    )
                if profession_mode or preset_stack_mode:
                    next_step = (
                        next_wizard_step(
                            template,
                            fields_def,
                            step + 1,
                            fields,
                            allow_stages=allow_stages,
                        )
                        if flow.get("modes")
                        else next_fillable_card_step(
                            template,
                            fields_def,
                            step + 1,
                            fields,
                            allow_stages=allow_stages,
                        )
                    )
                else:
                    stat_values = [
                        int(fields[f"stat_{item['key']}"])
                        for item in template["stats"]["attributes"]
                        if f"stat_{item['key']}" in fields
                    ]
                    if (
                        len(stat_values)
                        == len(template["stats"]["attributes"])
                        and sum(stat_values)
                        > int(template["stats"]["budget"])
                    ):
                        raise ValueError(
                            f"属性总值 {sum(stat_values)} 超过预算 "
                            f"{template['stats']['budget']}，"
                            "请重新建卡或调整模板"
                        )
                    next_step = (
                        next_wizard_step(
                            template,
                            fields_def,
                            step + 1,
                            fields,
                            allow_stages=allow_stages,
                        )
                        if flow.get("modes")
                        else next_fillable_card_step(
                            template,
                            fields_def,
                            step + 1,
                            fields,
                            allow_stages=allow_stages,
                        )
                    )
                if source_event_id:
                    fields[LAST_MESSAGE_KEY] = source_event_id
                connection.execute(
                    """
                    UPDATE character_card_drafts SET
                        fields_json = ?, current_step = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        json_dump(fields),
                        next_step,
                        now,
                        row["draft_id"],
                    ),
                )
                connection.execute(
                    """
                    UPDATE participants
                    SET card_status = 'draft', ready = 0, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, row["id"]),
                )
                self._insert_audit(
                    connection,
                    row["session_id"],
                    row["private_user_id"],
                    "card.field_update",
                    row["id"],
                    {
                        "field": definition["key"],
                        "step": next_step,
                        "dependency_cleared": dependency_check["cleared"],
                    },
                )
                connection.execute("COMMIT")
                result = {
                    "participant_id": row["id"],
                    "session_id": row["session_id"],
                    "fields": fields,
                    "template": template,
                    "current_step": next_step,
                    "complete": False,
                    "world": json_load(row["world_snapshot_json"], {}),
                }
                result["completion"] = wizard_completion_state(
                    template,
                    fields,
                    next_step,
                    allow_stages=allow_stages,
                )
                result["complete"] = bool(result["completion"]["complete"])
                if multi_presets:
                    result["selection_confirmation"] = {
                        "field_key": field_key,
                        "field_label": str(definition.get("label") or field_key),
                        "value_labels": [
                            str(
                                item.get("label")
                                or item.get("value")
                                or item.get("id")
                                or ""
                            )
                            for item in multi_presets
                        ],
                        "kind": "multi_preset",
                    }
                elif selected_preset:
                    result["selection_confirmation"] = {
                        "field_key": field_key,
                        "field_label": str(definition.get("label") or field_key),
                        "value_label": str(
                            selected_preset.get("label")
                            or selected_preset.get("value")
                            or selected_preset.get("id")
                            or ""
                        ),
                        "kind": "preset",
                    }
                if stack_resolved is not None and (
                    not stack_was_resolved
                    or field_key
                    in stat_generation_config(template).get(
                        "bonus_sources", []
                    )
                ):
                    result["stat_generation_result"] = stack_resolved
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def reset_card_draft_stats(
        self,
        private_origin: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._reset_card_draft_stats,
            private_origin,
        )

    def _reset_card_draft_stats(
        self,
        private_origin: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT pt.*, d.id AS draft_id, d.fields_json,
                           d.current_step, d.status AS draft_status,
                           d.expires_at AS draft_expires_at,
                           ic.world_snapshot_json
                    FROM participants pt
                    JOIN character_card_drafts d
                      ON d.participant_id = pt.id
                    JOIN instance_configs ic
                      ON ic.session_id = pt.session_id
                    JOIN sessions s ON s.id = pt.session_id
                    WHERE pt.private_origin = ? AND d.status = 'active'
                      AND s.state <> 'finished'
                    ORDER BY d.updated_at DESC LIMIT 1
                    """,
                    (private_origin,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError(
                        "当前私聊没有可调整的角色卡"
                    )
                now = utc_now()
                if (
                    row["draft_expires_at"]
                    and row["draft_expires_at"] <= now
                ):
                    connection.execute(
                        """
                        UPDATE character_card_drafts
                        SET status = 'expired', updated_at = ?
                        WHERE id = ?
                        """,
                        (now, row["draft_id"]),
                    )
                    raise ValueError("角色卡草稿已过期，请回群重新申请")
                world_snapshot = json_load(row["world_snapshot_json"], {})
                template = card_template(world_snapshot)
                fields = json_load(row["fields_json"], {})
                if not isinstance(fields, dict):
                    fields = {}
                world_obj = json_load(row["world_snapshot_json"], {})
                if uses_preset_stack_stats(template):
                    sources = stat_generation_config(template).get(
                        "bonus_sources", []
                    )
                    raise ValueError(
                        "本世界的属性已由预设锁定；请使用 /团 修改 "
                        + "、/团 修改 ".join(str(item) for item in sources)
                        + " 调整属性来源"
                    )
                if uses_profession_preset_stats(template):
                    profession_key = _field_key_for_semantic_role(
                        template, _ACTOR_PROFESSION_ROLE
                    )
                    primary_stat_key = _field_key_for_semantic_role(
                        template, _ACTOR_PRIMARY_STAT_ROLE
                    )
                    secondary_stat_key = _field_key_for_semantic_role(
                        template, _ACTOR_SECONDARY_STAT_ROLE
                    )
                    profession_name = str(
                        fields.get(profession_key) if profession_key else ""
                    )
                    if not profession_name:
                        raise ValueError("当前角色还没有选择职业")
                    # Keep profession, base stats and all text fields; only clear
                    # the primary/secondary choices and the derived total.
                    if primary_stat_key:
                        fields.pop(primary_stat_key, None)
                    if secondary_stat_key:
                        fields.pop(secondary_stat_key, None)
                    fields.pop("resolved_stat_total", None)
                    resolved = _resolve_semantic_profession_stats(
                        template, fields, require_complete=False
                    )
                    fields["profession_base_stats"] = resolved["base"]
                    for _k, _v in resolved["base"].items():
                        fields[f"stat_{_k}"] = _v
                    primary_step = next(
                        index
                        for index, _d in enumerate(
                            template.get("fields") or []
                        )
                        if isinstance(_d, Mapping)
                        and str(_d.get("key") or "") == primary_stat_key
                    )
                    target_step = primary_step
                    connection.execute(
                        """
                        UPDATE character_card_drafts SET
                            fields_json = ?, current_step = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (json_dump(fields), target_step, now, row["draft_id"]),
                    )
                    connection.execute(
                        """
                        UPDATE participants
                        SET card_status = 'draft', ready = 0, updated_at = ?
                        WHERE id = ?
                        """,
                        (now, row["id"]),
                    )
                    self._insert_audit(
                        connection,
                        row["session_id"],
                        row["private_user_id"],
                        "card.stats_reset",
                        row["id"],
                        {
                            "profession_reset": True,
                            "profession": profession_name,
                        },
                    )
                    connection.execute("COMMIT")
                    return {
                        "participant_id": row["id"],
                        "session_id": row["session_id"],
                        "fields": fields,
                        "template": template,
                        "current_step": target_step,
                        "complete": False,
                        "profession_reset": True,
                        "profession": profession_name,
                        "base_stats": dict(resolved["base"]),
                        "world": world_obj,
                    }
                allocation = card_stat_allocation(template, fields)
                stat_fields = allocation["stat_fields"]
                if not stat_fields:
                    raise ValueError("当前角色卡模板没有可分配数值")
                first_step = int(allocation["first_step"])
                has_stat_values = any(
                    item["field_key"] in fields
                    for item in stat_fields
                )
                if (
                    int(row["current_step"]) < first_step
                    and not has_stat_values
                ):
                    raise ValueError("尚未开始填写角色数值")
                removed = []
                for item in stat_fields:
                    field_key = str(item.get("field_key") or "")
                    if field_key in fields:
                        removed.append(field_key)
                        fields.pop(field_key, None)
                connection.execute(
                    """
                    UPDATE character_card_drafts SET
                        fields_json = ?, current_step = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        json_dump(fields),
                        first_step,
                        now,
                        row["draft_id"],
                    ),
                )
                connection.execute(
                    """
                    UPDATE participants
                    SET card_status = 'draft', ready = 0, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, row["id"]),
                )
                self._insert_audit(
                    connection,
                    row["session_id"],
                    row["private_user_id"],
                    "card.stats_reset",
                    row["id"],
                    {"removed_fields": removed},
                )
                connection.execute("COMMIT")
                return {
                    "participant_id": row["id"],
                    "session_id": row["session_id"],
                    "fields": fields,
                    "template": template,
                    "current_step": first_step,
                    "complete": False,
                    "world": json_load(row["world_snapshot_json"], {}),
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def previous_card_step(
        self, private_origin: str
    ) -> dict[str, Any]:
        return await self._run(
            self._reposition_card_draft, private_origin, ""
        )

    async def modify_card_field(
        self, private_origin: str, field_reference: str
    ) -> dict[str, Any]:
        return await self._run(
            self._reposition_card_draft,
            private_origin,
            field_reference,
        )

    def _reposition_card_draft(
        self, private_origin: str, field_reference: str
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT pt.*, d.id AS draft_id, d.fields_json,
                           d.current_step, ic.world_snapshot_json
                    FROM participants pt
                    JOIN character_card_drafts d ON d.participant_id = pt.id
                    JOIN instance_configs ic ON ic.session_id = pt.session_id
                    JOIN sessions s ON s.id = pt.session_id
                    WHERE pt.private_origin = ? AND d.status = 'active'
                      AND s.state <> 'finished'
                    ORDER BY d.updated_at DESC LIMIT 1
                    """,
                    (private_origin,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError(
                        "当前私聊没有进行中的角色卡"
                    )
                world = json_load(row["world_snapshot_json"], {})
                template = card_template(world)
                definitions = template["fields"]
                fields = json_load(row["fields_json"], {})
                fields = fields if isinstance(fields, dict) else {}
                reference = str(field_reference or "").strip().casefold()
                if reference:
                    candidates = [
                        (index, item)
                        for index, item in enumerate(definitions)
                        if reference
                        in {
                            str(item.get("key") or "").casefold(),
                            str(item.get("label") or "").casefold(),
                            str(item.get("label") or "")
                            .removeprefix("选择")
                            .split("（", 1)[0]
                            .casefold(),
                        }
                    ]
                    if len(candidates) != 1:
                        raise ValueError(
                            "未找到唯一字段，请使用完整字段名称"
                        )
                    target_step, definition = candidates[0]
                else:
                    current = min(
                        max(0, int(row["current_step"])), len(definitions)
                    )
                    candidates = [
                        (index, item)
                        for index, item in enumerate(definitions[:current])
                        if field_visible(item, fields)
                    ]
                    if not candidates:
                        raise ValueError("已经是第一个建卡步骤")
                    target_step, definition = candidates[-1]
                if not field_visible(definition, fields):
                    raise ValueError("该字段在当前角色选择下不需要填写")
                field_key = str(definition.get("key") or "")
                clear_field_and_dependents(template, fields, field_key)
                if (
                    uses_preset_stack_stats(template)
                    and field_key
                    in stat_generation_config(template).get(
                        "bonus_sources", []
                    )
                ):
                    clear_generated_stats(template, fields)
                profession_key = _field_key_for_semantic_role(
                    template, _ACTOR_PROFESSION_ROLE
                )
                if profession_key and field_key == profession_key:
                    fields.pop("profession_base_stats", None)
                    fields.pop("resolved_stat_total", None)
                    for attribute in template.get("stats", {}).get(
                        "attributes", []
                    ):
                        fields.pop(f"stat_{attribute.get('key')}", None)
                    primary_stat_key = _field_key_for_semantic_role(
                        template, _ACTOR_PRIMARY_STAT_ROLE
                    )
                    secondary_stat_key = _field_key_for_semantic_role(
                        template, _ACTOR_SECONDARY_STAT_ROLE
                    )
                    if primary_stat_key:
                        fields.pop(primary_stat_key, None)
                    if secondary_stat_key:
                        fields.pop(secondary_stat_key, None)
                fields.pop(LAST_MESSAGE_KEY, None)
                now = utc_now()
                connection.execute(
                    """
                    UPDATE character_card_drafts
                    SET fields_json = ?, current_step = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (json_dump(fields), target_step, now, row["draft_id"]),
                )
                self._insert_audit(
                    connection,
                    row["session_id"],
                    row["private_user_id"],
                    "card.step_reposition",
                    row["id"],
                    {"field": field_key, "step": target_step},
                )
                connection.execute("COMMIT")
                return {
                    "participant_id": row["id"],
                    "session_id": row["session_id"],
                    "fields": fields,
                    "template": template,
                    "current_step": target_step,
                    "complete": False,
                    "world": world,
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def confirm_card_draft(
        self,
        private_origin: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._confirm_card_draft,
            private_origin,
        )
