from __future__ import annotations

from .characters_support import *


class CharacterCardsQueriesRepositoryMixin:
    def _confirm_card_draft(
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
                           ic.world_snapshot_json, ic.time_rules_json,
                           ic.world_revision
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
                    raise DatabaseNotFoundError("当前私聊没有可确认的角色卡")
                world_snapshot = json_load(row["world_snapshot_json"], {})
                template = card_template(world_snapshot)
                fields = json_load(row["fields_json"], {})
                if not isinstance(fields, dict):
                    fields = {}
                dependency_check = revalidate_dependent_selections(
                    template,
                    fields,
                )
                dependency_issues = [
                    *dependency_check.get("cleared", []),
                    *dependency_check.get("needs_revision", []),
                ]
                if staged_creation(template):
                    stage_by_key = {
                        str(definition.get("key") or ""): field_stage(definition)
                        for definition in template.get("fields") or []
                        if isinstance(definition, Mapping)
                    }
                    dependency_issues = [
                        item
                        for item in dependency_issues
                        if stage_by_key.get(str(item.get("field") or ""))
                        == CARD_STAGE_A
                    ]
                if dependency_issues:
                    cleared_keys = {
                        str(item.get("field") or "")
                        for item in dependency_issues
                    }
                    first_step = next(
                        (
                            index
                            for index, definition in enumerate(template["fields"])
                            if str(definition.get("key") or "") in cleared_keys
                        ),
                        min(
                            max(0, int(row["current_step"])),
                            len(template["fields"]),
                        ),
                    )
                    now = utc_now()
                    connection.execute(
                        """
                        UPDATE character_card_drafts
                        SET fields_json = ?, current_step = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            json_dump(fields),
                            first_step,
                            now,
                            row["draft_id"],
                        ),
                    )
                    self._insert_audit(
                        connection,
                        row["session_id"],
                        row["private_user_id"],
                        "card.dependencies_revalidated",
                        row["id"],
                        {
                            "cleared": dependency_check.get("cleared", []),
                            "retained": dependency_check.get("retained", []),
                            "needs_revision": dependency_check.get(
                                "needs_revision", []
                            ),
                            "target_step": first_step,
                        },
                    )
                    connection.execute("COMMIT")
                    return {
                        "participant_id": row["id"],
                        "session_id": row["session_id"],
                        "fields": fields,
                        "template": template,
                        "current_step": first_step,
                        "complete": False,
                        "needs_revision": True,
                        "dependency_issues": dependency_issues,
                        "world": world_snapshot,
                    }
                # D1：确认前先计算阶段状态（依赖 _staged_fields 内部标记）。
                stage_state = card_stage_state(template, fields)
                staged = staged_creation(template)
                opening_field_definitions = [
                    definition
                    for definition in template.get("fields") or []
                    if isinstance(definition, Mapping)
                    and (not staged or field_stage(definition) == CARD_STAGE_A)
                ]
                opening_fields = (
                    stage_field_projection(
                        template,
                        fields,
                        stages=(CARD_STAGE_A,),
                    )
                    if staged
                    else fields
                )
                fields.pop("_alloc", None)
                fields.pop("_wizard_pages", None)
                fields.pop("_wizard_delivery", None)
                fields.pop(CANDIDATE_SNAPSHOTS_KEY, None)
                fields.pop("_creation_mode", None)
                fields.pop("_archetype_id", None)
                fields.pop(LAST_MESSAGE_KEY, None)
                # B2（A5）：预设-only 强制守卫——非自由字段的值必须是合法预设。
                # 分阶段世界首次确认只校验 A 组。B/C 组即使已由原型包代填、
                # 只填写了一部分，或仍显示“剧情中补充”，也只能进入待补充，
                # 不能阻断开演。
                preset_only_guard(template, opening_fields)
                item_grant_plan = card_item_grants(
                    world_snapshot,
                    opening_fields,
                    strict=True,
                )
                for definition in template["fields"]:
                    key = str(definition["key"])
                    if staged and field_stage(definition) != CARD_STAGE_A:
                        continue
                    if key not in fields:
                        continue
                    options = preset_options(template, definition, fields)
                    fields[key] = validate_card_field(
                        definition,
                        fields[key],
                        options,
                    )
                # D1：分阶段世界只要求 A 组完整即可提交审核/开演；
                # B/C 组必填字段进入待补充状态，不阻塞确认。
                missing_items = stage_required_missing(template, fields)
                missing = [
                    str(item.get("label") or item.get("key") or "")
                    for item in missing_items
                ]
                if missing:
                    raise ValueError("尚未填写：" + "、".join(missing))
                if template.get("preset_dimensions"):
                    opening_preset_keys = {
                        str(definition.get("key") or "")
                        for definition in opening_field_definitions
                        if definition.get("preset_source")
                    }
                    validate_preset_selection(
                        template,
                        opening_fields,
                        require_complete=True,
                        required_dimension_ids=opening_preset_keys,
                    )
                    fields["_resolved_boundaries"] = resolve_character_presets(
                        world_snapshot,
                        opening_fields,
                    )
                name_definition = _field_for_semantic_role(
                    template, _ACTOR_NAME_ROLE
                )
                alias_definition = _field_for_semantic_role(
                    template, _ACTOR_ALIAS_ROLE
                )
                if name_definition is None or alias_definition is None:
                    raise ValueError("角色模板缺少姓名或代号语义字段")
                character_name = clean_card_field(
                    fields.get(str(name_definition["key"])),
                    label=str(name_definition.get("label") or "角色姓名"),
                    max_chars=12,
                )
                character_code = clean_card_field(
                    fields.get(str(alias_definition["key"])),
                    label=str(alias_definition.get("label") or "副本代号"),
                    max_chars=12,
                )
                if not character_name or not character_code:
                    raise ValueError("角色姓名与副本代号不能为空")
                duplicate = connection.execute(
                    """
                    SELECT id FROM participants
                    WHERE session_id = ? AND id <> ?
                      AND participation_status NOT IN ('retired', 'archived')
                      AND (
                           lower(character_name) = lower(?)
                        OR lower(character_code) = lower(?)
                      )
                    LIMIT 1
                    """,
                    (
                        row["session_id"],
                        row["id"],
                        character_name,
                        character_code,
                    ),
                ).fetchone()
                if duplicate:
                    raise ValueError("角色姓名或副本代号已被使用")
                stat_definition = template["stats"]
                if uses_preset_stack_stats(template):
                    calculated_stats = calculate_preset_stack_stats(
                        template,
                        fields,
                        require_complete=True,
                    )
                    assert calculated_stats is not None
                    for key, expected_value in calculated_stats["raw"].items():
                        field_name = f"stat_{key}"
                        if field_name in fields and int(fields[field_name]) != expected_value:
                            raise ValueError(
                                f"{calculated_stats['labels'][key]}数值与预设来源不一致"
                            )
                    resolved_stats = sync_preset_stack_fields(
                        template,
                        fields,
                        require_complete=True,
                    )
                    assert resolved_stats is not None
                elif uses_profession_preset_stats(template):
                    resolved_stats = _resolve_semantic_profession_stats(
                        template,
                        fields,
                        require_complete=True,
                    )
                    for key, expected_value in resolved_stats[
                        "raw"
                    ].items():
                        actual_value = int(
                            fields.get(f"stat_{key}", -999)
                        )
                        if actual_value != expected_value:
                            raise ValueError(
                                f"{resolved_stats['labels'][key]}"
                                "数值与职业基础属性及主副属性加成不一致，"
                                "请使用「重填数值」重新生成"
                            )
                    final_total = int((stat_definition.get("total_validation") or {}).get("final_total", stat_definition.get("budget", 0)))
                    if int(resolved_stats["effective_total"]) != final_total:
                        raise ValueError(f"角色最终属性总和必须为{final_total}")
                    resolved_stats["budget"] = final_total
                    fields["profession_base_stats"] = dict(
                        resolved_stats["base"]
                    )
                    fields["resolved_stat_total"] = int(
                        resolved_stats["effective_total"]
                    )
                else:
                    raw_stats: dict[str, int] = {}
                    labels: dict[str, str] = {}
                    modifiers: dict[str, int] = {}
                    for attribute in stat_definition["attributes"]:
                        key = str(attribute["key"])
                        value = int(
                            fields.get(
                                f"stat_{key}",
                                attribute.get("default", 0),
                            )
                        )
                        if not int(attribute["minimum"]) <= value <= int(
                            attribute["maximum"]
                        ):
                            raise ValueError(
                                f"{attribute['label']}超出模板允许范围"
                            )
                        raw_stats[key] = value
                        labels[key] = str(attribute["label"])
                        modifiers[key] = int(
                            stat_definition["modifier_table"].get(
                                str(value),
                                0,
                            )
                        )
                    allocation = card_stat_allocation(template, fields)
                    if not allocation.get("total_ok", True):
                        rule = allocation.get("allocation_rule", "maximum")
                        if rule == "exact": raise ValueError("角色属性总值必须刚好等于世界模板预算")
                        if rule == "range": raise ValueError("角色属性总值不在允许区间")
                        raise ValueError("角色属性总值超过世界模板预算")
                    resolved_stats = {
                        "raw": raw_stats,
                        "labels": labels,
                        "modifiers": modifiers,
                        "budget": int(stat_definition["budget"]),
                        "modifier_table": dict(
                            stat_definition["modifier_table"]
                        ),
                    }
                now = utc_now()
                card_id = row["character_card_id"] or new_id("pcard")
                existing_card = connection.execute(
                    "SELECT * FROM character_cards WHERE id = ?",
                    (card_id,),
                ).fetchone()
                if not existing_card:
                    version_no = 1
                    connection.execute(
                        """
                        INSERT INTO character_cards(
                            id, owner_user_id, world_id, display_name,
                            archived, deleted, current_version,
                            created_at, updated_at
                        )
                        SELECT ?, ?, s.world_id, ?, 0, 0, 1, ?, ?
                        FROM sessions s WHERE s.id = ?
                        """,
                        (
                            card_id,
                            row["group_user_id"],
                            character_name,
                            now,
                            now,
                            row["session_id"],
                        ),
                    )
                else:
                    version_no = int(existing_card["current_version"]) + 1
                    connection.execute(
                        """
                        UPDATE character_cards SET
                            display_name = ?, current_version = ?,
                            archived = 0, updated_at = ?
                        WHERE id = ?
                        """,
                        (character_name, version_no, now, card_id),
                    )
                status = (
                    CARD_APPROVED
                    if template["auto_approve"]
                    else CARD_PENDING
                )
                version_id = new_id("pcardv")
                connection.execute(
                    """
                    INSERT INTO character_card_versions(
                        id, character_card_id, version_no, template_version,
                        profile_json, stats_json, status, review_note,
                        reviewed_by, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, ?)
                    """,
                    (
                        version_id,
                        card_id,
                        version_no,
                        template["version"],
                        json_dump(fields),
                        json_dump(resolved_stats),
                        status,
                        "system" if status == CARD_APPROVED else "",
                        now,
                    ),
                )
                initial_runtime_state: dict[str, Any] = {}
                protocol = world_snapshot.get("protocol")
                protocol = protocol if isinstance(protocol, Mapping) else {}
                features = protocol.get("features")
                features = features if isinstance(features, Mapping) else {}
                rules_map = world_snapshot.get("rules")
                rules_map = rules_map if isinstance(rules_map, Mapping) else {}
                # B1：协议 features 之外，直接按 rules 声明判断（能力/资源目录真实存在）。
                has_resources = "resources" in features or isinstance(rules_map.get("resources"), Mapping)
                has_capabilities = (
                    "capability_effects" in features
                    or isinstance(
                        rules_map.get("capability_effects"),
                        Mapping,
                    )
                )
                if has_resources:
                    resource_module = module_value(world_snapshot, "resources", {})
                    resource_module = resource_module if isinstance(resource_module, Mapping) else {}
                    definitions = resource_module.get("definitions", resource_module.get("items", []))
                    if isinstance(definitions, Sequence) and not isinstance(definitions, (str, bytes)):
                        refs: dict[str, Any] = {}
                        for definition in definitions:
                            if not isinstance(definition, Mapping):
                                continue
                            resource_id = str(definition.get("resource_id") or definition.get("id") or "")
                            if resource_id and "initial_value" in definition:
                                refs[f"resource:{resource_id}"] = definition["initial_value"]
                        if refs:
                            initial_runtime_state["refs"] = refs
                            # D1 Schema 20：职业资源权威落库
                            # （character_resources，D1-DATA-005/006）。
                            for resource_ref, initial_value in refs.items():
                                resource_id = str(resource_ref).split(":", 1)[-1]
                                resource_def = next(
                                    (
                                        item
                                        for item in definitions
                                        if str(item.get("resource_id") or item.get("id") or "")
                                        == resource_id
                                    ),
                                    {},
                                )
                                connection.execute(
                                    """
                                    INSERT OR REPLACE INTO character_resources(
                                        id, session_id, character_id, resource_ref,
                                        label, current, maximum, state_json,
                                        created_at, updated_at
                                    ) VALUES (?, ?, ?, ?, ?, ?, ?, '{}', ?, ?)
                                    """,
                                    (
                                        new_id("resource"),
                                        row["session_id"],
                                        row["id"],
                                        resource_ref,
                                        str(resource_def.get("label") or resource_id)[:120],
                                        max(0, int(initial_value or 0)),
                                        max(
                                            0,
                                            int(
                                                resource_def.get("maximum")
                                                or initial_value
                                                or 0
                                            ),
                                        ),
                                        now,
                                        now,
                                    ),
                                )
                if has_capabilities:
                    registry = EntityRegistry(world_snapshot)
                    service = CapabilityService(world_snapshot, registry)
                    preset_values: dict[str, Any] = {}
                    preset_source_map: dict[str, str] = {}
                    preset_refs = fields.get("_preset_refs", {})
                    if isinstance(preset_refs, Mapping):
                        for dimension, selected in preset_refs.items():
                            # 单选与多选统一进入授予条件。
                            # 多选把每个稳定 ID 转成规范选择引用，并保留集合供
                            # 条件引擎的 contains/intersects 求值（§2.3/§27.2）。
                            if isinstance(selected, Mapping):
                                option_id = str(
                                    selected.get("id")
                                    or selected.get("snapshot", {}).get("id")
                                    or ""
                                )
                                if option_id:
                                    preset_values[f"custom:preset.{dimension}"] = option_id
                                    preset_source_map[option_id] = str(dimension)
                            elif isinstance(selected, Sequence) and not isinstance(selected, (str, bytes)):
                                ids: list[str] = []
                                for option in selected:
                                    if not isinstance(option, Mapping):
                                        continue
                                    option_id = str(
                                        option.get("id")
                                        or option.get("snapshot", {}).get("id")
                                        or ""
                                    )
                                    if not option_id:
                                        continue
                                    ids.append(option_id)
                                    preset_source_map[option_id] = str(dimension)
                                    preset_values[f"custom:preset.{dimension}:{option_id}"] = option_id
                                if ids:
                                    preset_values[f"custom:preset.{dimension}"] = ids
                    actor_ref = f"character:{row['id']}"
                    migration_operation_id = (
                        f"card_capabilities:{row['session_id']}:{row['id']}:{row['world_revision']}"
                    )
                    grants = service.initial_grants(preset_values)
                    granted: list[str] = []
                    seen_grant_sources: dict[str, list[str]] = {}
                    for grant in grants:
                        capability_ref = str(grant.get("capability_ref") or grant.get("target_ref") or "")
                        source_ref = str(grant.get("source_ref") or f"card:{capability_ref}")
                        # 集合27.2集合
                        matched_dimensions = sorted({
                            key.split(":", 2)[1]
                            for key in grant.get("preset_keys", [])
                            if len(key.split(":", 2)) >= 3
                        }) or sorted({
                            dim for option_id, dim in preset_source_map.items()
                            if option_id == capability_ref
                        })
                        dimension_label = ".".join(matched_dimensions) or "character_card"
                        state_json = {
                            "sources": seen_grant_sources.setdefault(capability_ref, []),
                            "dimensions": matched_dimensions,
                            "field": dimension_label,
                            "template_version": str(template.get("version") or ""),
                            "world_revision": int(row["world_revision"] or 0),
                        }
                        if dimension_label not in state_json["sources"]:
                            state_json["sources"].append(dimension_label)
                        connection.execute(
                            """
                            INSERT OR IGNORE INTO actor_capability_instances(
                                id, session_id, actor_ref, capability_ref,
                                definition_version, source_ref, state_json,
                                persistence_scope, available, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, 1, ?, ?)
                            """,
                            (
                                new_id("capability_instance"), row["session_id"], actor_ref,
                                capability_ref, source_ref,
                                json_dump(state_json),
                                str(grant.get("persistence_scope") or "campaign"), now, now,
                            ),
                        )
                        # D1 Schema 20：确认时能力权威落库
                        # （character_capabilities，D1-DATA-006）。
                        connection.execute(
                            """
                            INSERT OR REPLACE INTO character_capabilities(
                                id, session_id, character_id, capability_ref,
                                source_ref, state_json, available,
                                created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                            """,
                            (
                                new_id("capability"),
                                row["session_id"],
                                row["id"],
                                capability_ref,
                                source_ref,
                                json_dump(state_json),
                                now,
                                now,
                            ),
                        )
                        granted.append(capability_ref)
                    migration_payload = {
                        "character_card_version_id": version_id,
                        "world_revision": int(row["world_revision"]),
                        "actor_ref": actor_ref,
                        "preset_values": preset_values,
                        "granted_capabilities": granted,
                    }
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO migration_receipts(
                            id, migration_type, source_version, target_version,
                            session_id, operation_id, receipt_json, confirmed_by, created_at
                        ) VALUES (?, 'character_capabilities', ?, 'v5', ?, ?, ?, ?, ?)
                        """,
                        (
                            new_id("migration"), str(template.get("version") or ""),
                            row["session_id"], migration_operation_id,
                            json_dump(migration_payload), row["private_user_id"], now,
                        ),
                    )
                participation_status = (
                    PARTICIPANT_ACTIVE
                    if status == CARD_APPROVED
                    else PARTICIPANT_RESERVED
                )
                connection.execute(
                    """
                    UPDATE participants SET
                        character_card_id = ?, character_version_id = ?,
                        character_name = ?, character_code = ?,
                        card_status = ?, ready = 0,
                        participation_status = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        card_id,
                        version_id,
                        character_name,
                        character_code,
                        status,
                        participation_status,
                        now,
                        row["id"],
                    ),
                )
                if status == CARD_APPROVED:
                    fate_participant = connection.execute(
                        "SELECT * FROM participants WHERE id = ?",
                        (row["id"],),
                    ).fetchone()
                    if fate_participant is None:
                        raise DatabaseNotFoundError(
                            "角色卡确认后无法读取参与者记录"
                        )
                    self._initialize_player_fate_locked(
                        connection,
                        participant=dict(fate_participant),
                        world=world_snapshot,
                        now=now,
                    )
                self._sync_card_stage_column(
                    connection,
                    row["id"],
                    stage_state["stage"],
                )
                connection.execute(
                    """
                    UPDATE character_card_drafts
                    SET status = 'submitted', updated_at = ?
                    WHERE id = ?
                    """,
                    (now, row["draft_id"]),
                )
                connection.execute(
                    """
                    UPDATE players SET
                        character_name = ?, profile_json = ?,
                        enabled = 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        character_name,
                        json_dump(fields),
                        now,
                        row["player_id"],
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO character_runtime_states(
                        id, session_id, participant_id, character_card_id,
                        state_json, revision, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(session_id, participant_id) DO UPDATE SET
                        character_card_id = excluded.character_card_id,
                        revision = revision + 1,
                        updated_at = excluded.updated_at
                    """,
                    (
                        new_id("runtime"),
                        row["session_id"],
                        row["id"],
                        card_id,
                        json_dump(initial_runtime_state),
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    UPDATE timer_instances
                    SET status = 'completed', updated_at = ?
                    WHERE participant_id = ?
                      AND timer_type = 'card_completion'
                      AND status IN ('active', 'paused')
                    """,
                    (now, row["id"]),
                )
                if status == CARD_APPROVED:
                    time_rules = normalize_time_rules(
                        json_load(row["time_rules_json"], {})
                    )
                    self._create_timer(
                        connection,
                        session_id=row["session_id"],
                        participant_id=row["id"],
                        timer_type="ready",
                        timeout_seconds=time_rules["ready_timeout_seconds"],
                        reminder_seconds=None,
                        action={
                            "timeout_action": time_rules[
                                "ready_timeout_action"
                            ]
                        },
                    )
                # C6：自动审批在角色卡事务内发放；人工审批等首次批准。
                seeded_items: list[str] = []
                if status == CARD_APPROVED:
                    grants = []
                    for grant in item_grant_plan.get("grants", []):
                        if not isinstance(grant, Mapping):
                            continue
                        scope = str(grant.get("owner_scope") or "character")
                        grants.append(
                            {
                                **dict(grant),
                                "owner_type": scope,
                                "owner_ref": (
                                    str(row["id"])
                                    if scope == "character"
                                    else f"party:{row['session_id']}"
                                ),
                            }
                        )
                    receipt = self._grant_item_instances_locked(
                        connection,
                        session_id=str(row["session_id"]),
                        grants=grants,
                        operation_id=(
                            f"card_start_items:{row['session_id']}:{row['id']}"
                        ),
                        actor_id=str(row["private_user_id"]),
                        audit_action="card.items_granted",
                    )
                    seeded_items = [
                        f"『{grant.get('item_label') or grant.get('item_id')}』"
                        f" ×{grant.get('quantity')}"
                        for grant in receipt.get("granted", [])
                        if isinstance(grant, Mapping)
                    ]

                # 1.0.0-A7：角色初始钱包幂等播种（rules.economy.initial_wallets，
                # owner_type 为 character 的项目按参与者展开）。
                seeded_funds: list[str] = []
                if status == CARD_APPROVED:
                    from ..world_contract import world_contract as _contract
                    _econ = _contract(world_snapshot).get("economy") or {}
                    _init_wallets = _econ.get("initial_wallets") or []
                    if isinstance(_init_wallets, list):
                        for _wallet in _init_wallets:
                            if not isinstance(_wallet, Mapping):
                                continue
                            _owt = str(
                                _wallet.get("owner_type") or "character"
                            ).strip().lower()
                            if _owt not in {"character", "player"}:
                                continue
                            _cid = str(_wallet.get("currency_id") or "").strip()
                            if not _cid:
                                continue
                            _precision = self._currency_precision(
                                connection, row["session_id"], _cid
                            )
                            _amount = _money_to_minor(
                                _wallet.get("amount") or 0,
                                _precision,
                            )
                            if _amount <= 0:
                                continue
                            self._ensure_wallet(
                                connection,
                                row["session_id"],
                                "character",
                                str(row["id"]),
                                _cid,
                                initial=_amount,
                            )
                            _currency = connection.execute(
                                """
                                SELECT name, short_name, icon, precision
                                FROM economy_currencies
                                WHERE session_id = ? AND currency_id = ?
                                """,
                                (row["session_id"], _cid),
                            ).fetchone()
                            seeded_funds.append(
                                format_money(
                                    _cid,
                                    _amount,
                                    precision=(
                                        int(_currency["precision"])
                                        if _currency
                                        else int(_precision)
                                    ),
                                    label=(
                                        str(_currency["name"])
                                        if _currency
                                        else ""
                                    ),
                                    short_label=(
                                        str(_currency["short_name"])
                                        if _currency
                                        else ""
                                    ),
                                    icon=(
                                        str(_currency["icon"])
                                        if _currency
                                        else ""
                                    ),
                                )
                            )
                confirmed_copy, entry_beat = _character_lifecycle_copy(
                    world_snapshot,
                    character_name=character_name,
                )
                append_event(
                    connection,
                    session_id=row["session_id"],
                    turn_no=(
                        row["turn_no"] if "turn_no" in row.keys() else 0
                    ),
                    role="system",
                    actor_id="system",
                    actor_name="角色创建",
                    content=confirmed_copy,
                    meta={
                        "kind": "character_confirmed",
                        "participant_id": row["id"],
                        "seeded_items": seeded_items,
                        "seeded_funds": seeded_funds,
                    },
                    created_at=now,
                )
                self._insert_audit(
                    connection,
                    row["session_id"],
                    row["private_user_id"],
                    "card.confirm",
                    row["id"],
                    {
                        "version": version_no,
                        "status": status,
                        "seeded_starter_loadout": seeded_items,
                        "seeded_funds": seeded_funds,
                    },
                )
                updated = connection.execute(
                    "SELECT * FROM participants WHERE id = ?",
                    (row["id"],),
                ).fetchone()
                connection.execute("COMMIT")
                result = self._participant(updated)
                result["auto_approved"] = status == CARD_APPROVED
                result["seeded_starter_loadout"] = list(seeded_items)
                result["seeded_funds"] = list(seeded_funds)
                result["card_stage"] = stage_state["stage"]
                result["card_stage_pending_count"] = int(
                    stage_state["pending_count"]
                )
                result["card_stage_complete"] = bool(
                    stage_state["complete"]
                )
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def cancel_card_draft(
        self,
        private_origin: str,
    ) -> dict[str, Any]:
        return await self._run(self._cancel_card_draft, private_origin)

    def _cancel_card_draft(self, private_origin: str) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT pt.*, d.id AS draft_id
                    FROM participants pt
                    JOIN character_card_drafts d
                      ON d.participant_id = pt.id
                    JOIN sessions s ON s.id = pt.session_id
                    WHERE pt.private_origin = ? AND d.status = 'active'
                      AND s.state <> 'finished'
                    """,
                    (private_origin,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("当前没有进行中的角色卡")
                now = utc_now()
                connection.execute(
                    """
                    UPDATE character_card_drafts
                    SET status = 'cancelled',
                        cancel_reason = 'player_cancelled',
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (now, row["draft_id"]),
                )
                connection.execute(
                    """
                    UPDATE participants
                    SET participation_status = 'reserved',
                        card_status = 'uncreated', ready = 0,
                        exit_reason = '', updated_at = ?
                    WHERE id = ?
                    """,
                    (now, row["id"]),
                )
                connection.execute(
                    """
                    UPDATE card_binding_codes
                    SET status = 'cancelled',
                        failure_reason = 'draft_cancelled'
                    WHERE participant_id = ? AND status = 'active'
                    """,
                    (row["id"],),
                )
                connection.execute(
                    """
                    UPDATE timer_instances
                    SET status = 'cancelled', deadline_at = '',
                        reminder_at = '', updated_at = ?
                    WHERE participant_id = ?
                      AND timer_type = 'card_completion'
                      AND status IN ('active', 'paused')
                    """,
                    (now, row["id"]),
                )
                self._insert_audit(
                    connection,
                    row["session_id"],
                    row["private_user_id"],
                    "card.cancel",
                    row["id"],
                    {},
                )
                connection.execute("COMMIT")
                return {
                    "participant_id": row["id"],
                    "session_id": row["session_id"],
                    "seat_reserved": True,
                    "card_status": CARD_UNCREATED,
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def restart_card_draft(
        self,
        private_origin: str,
    ) -> dict[str, Any]:
        return await self._run(self._restart_card_draft, private_origin)
