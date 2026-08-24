from __future__ import annotations

from .supplement_support import *


class SupplementActionsRepositoryMixin:
    async def maybe_open_supplement_offers(
        self,
        session_id: str,
        *,
        turn_no: int | None = None,
        current_scene: str = "",
        chapter: str = "",
        trigger_source: str = "round_window",
        actor: str = "system",
    ) -> dict[str, Any]:
        """回合提交后调用：按轮次/章节策略为合格玩家打开补充提议。"""

        return await self._run(
            self._maybe_open_supplement_offers,
            str(session_id or "").strip(),
            turn_no,
            str(current_scene or ""),
            str(chapter or ""),
            str(trigger_source or "round_window"),
            str(actor or "system"),
        )

    def _maybe_open_supplement_offers(
        self,
        session_id: str,
        turn_no: int | None,
        current_scene: str,
        chapter: str,
        trigger_source: str,
        actor: str,
    ) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "enabled": False,
            "trigger": trigger_source,
            "turn_no": 0,
            "opened": [],
            "skipped": [],
            "opened_count": 0,
        }
        if not session_id:
            return summary
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not session:
                    connection.execute("COMMIT")
                    return summary
                if str(session["state"] or "") == SESSION_FINISHED:
                    connection.execute("COMMIT")
                    return summary
                if turn_no is None:
                    turn_no = int(session["turn_no"] or 0)
                config_row = connection.execute(
                    "SELECT world_snapshot_json FROM instance_configs WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                world = json_load(config_row["world_snapshot_json"], {}) if config_row else {}
                template = card_template(world)
                if not staged_creation(template):
                    connection.execute("COMMIT")
                    return summary
                config = supplement_config(world)
                trigger = effective_trigger(
                    trigger_source,
                    turn_no=turn_no,
                    chapter=chapter,
                    config=config,
                )
                summary["enabled"] = True
                summary["trigger"] = trigger
                summary["turn_no"] = int(turn_no or 0)
                participant_rows = connection.execute(
                    """
                    SELECT pt.*, ccv.profile_json
                    FROM participants pt
                    JOIN character_card_versions ccv
                      ON ccv.id = pt.character_version_id
                    WHERE pt.session_id = ?
                      AND pt.card_status = 'approved'
                      AND pt.participation_status NOT IN ('retired', 'archived')
                    """,
                    (session_id,),
                ).fetchall()
                offer_rows = connection.execute(
                    """
                    SELECT * FROM delivery_outbox
                    WHERE session_id = ? AND kind = ?
                    ORDER BY created_at ASC, id ASC
                    """,
                    (session_id, SUPPLEMENT_KIND),
                ).fetchall()
                latest_by_field: dict[tuple[str, str], Any] = {}
                for offer_row in offer_rows:
                    meta = json_load(offer_row["meta_json"], {})
                    if str(meta.get("kind") or "") != SUPPLEMENT_KIND:
                        continue
                    key = (
                        str(meta.get("participant_id") or ""),
                        str(meta.get("field_key") or ""),
                    )
                    previous = latest_by_field.get(key)
                    if previous is None or str(offer_row["created_at"]) >= str(
                        previous["created_at"]
                    ):
                        latest_by_field[key] = offer_row
                context = {
                    "session": {
                        "turn_no": int(turn_no or 0),
                        "state": str(session["state"] or ""),
                    },
                    "scene": {"ref": current_scene},
                    "custom": {
                        "runtime": {
                            "current_scene": current_scene,
                            "chapter": chapter,
                        }
                    },
                }
                for participant in participant_rows:
                    participant_id = str(participant["id"] or "")
                    profile = json_load(participant["profile_json"], {})
                    missing = missing_bc_fields(template, profile)
                    if not missing:
                        continue
                    stage_ordinals: dict[str, int] = {}
                    for field in missing:
                        stage = field_stage(field)
                        stage_ordinals[str(field.get("key") or "")] = (
                            stage_ordinals.get(stage, 0)
                        )
                        stage_ordinals[stage] = (
                            stage_ordinals.get(stage, 0) + 1
                        )
                    active_count = 0
                    for (owner_id, _field_key), offer_row in latest_by_field.items():
                        if owner_id != participant_id:
                            continue
                        offer_meta = json_load(offer_row["meta_json"], {})
                        if str(offer_meta.get("state") or "") in OFFER_OPEN_STATES:
                            if not offer_expired(offer_meta, turn_no, config):
                                active_count += 1
                    opened_any = False
                    for ordinal, field in enumerate(missing):
                        field_key = str(field.get("key") or "")
                        stage_ordinal = stage_ordinals.get(field_key, 0)
                        if (
                            trigger == "round_window"
                            and int(turn_no or 0)
                            < field_open_round(field, stage_ordinal, config)
                        ):
                            continue
                        existing = latest_by_field.get((participant_id, field_key))
                        existing_meta = (
                            json_load(existing["meta_json"], {})
                            if existing is not None
                            else None
                        )
                        if not field_is_reofferable(
                            existing_meta,
                            turn_no,
                            trigger,
                            config,
                        ):
                            continue
                        if not condition_matches(
                            field,
                            world=world,
                            context=context,
                        ):
                            continue
                        if trigger == "round_window":
                            if opened_any:
                                break
                            if active_count >= int(config["max_active_offers"]):
                                break
                        result = self._open_offer_locked(
                            connection,
                            session=session,
                            participant=participant,
                            template=template,
                            world=world,
                            profile=profile,
                            field=field,
                            ordinal=stage_ordinal,
                            config=field_supplement_config(
                                field,
                                config,
                                world,
                            ),
                            turn_no=int(turn_no or 0),
                            trigger_source=trigger,
                            actor=actor,
                        )
                        if result is not None:
                            summary["opened"].append(result)
                            opened_any = True
                            active_count += 1
                            latest_by_field[(participant_id, field_key)] = {
                                "created_at": str(result["created_at"]),
                                "meta_json": json_dump(result["meta"]),
                            }
                        else:
                            summary["skipped"].append(
                                {
                                    "participant_id": participant_id,
                                    "field_key": field_key,
                                }
                            )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        summary["opened_count"] = len(summary["opened"])
        return summary

    # ------------------------------------------------------------------
    # 列表
    # ------------------------------------------------------------------

    async def confirm_supplement_offer(
        self,
        session_id: str,
        offer_id: str,
        *,
        candidate_ids: Sequence[str] | None = None,
        candidate_indexes: Sequence[int] | None = None,
        text_value: str = "",
        actor: str = "",
        private_origin: str = "",
        expected_revision: int | None = None,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        """玩家确认补充：同事务写新角色版本、物品、公开投影并关闭提议。"""

        return await self._run(
            self._confirm_supplement_offer,
            str(session_id or "").strip(),
            str(offer_id or "").strip(),
            [str(item) for item in (candidate_ids or [])],
            [int(item) for item in (candidate_indexes or [])],
            str(text_value or ""),
            str(actor or ""),
            str(private_origin or ""),
            expected_revision,
            str(idempotency_key or ""),
        )

    def _confirm_supplement_offer(
        self,
        session_id: str,
        offer_id: str,
        candidate_ids: list[str],
        candidate_indexes: list[int],
        text_value: str,
        actor: str,
        private_origin: str,
        expected_revision: int | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        expected = require_expected_revision(
            expected_revision,
            label="角色补充版本",
        )
        request_key = require_idempotency_key(idempotency_key)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                row = self._offer_row_locked(connection, session_id, offer_id)
                meta = json_load(row["meta_json"], {})
                participant, session, world, template, config = (
                    self._load_offer_context_locked(
                        connection,
                        session_id,
                        row,
                        meta,
                    )
                )
                turn_no = int(session["turn_no"] or 0)
                self._assert_offer_owner_locked(
                    connection,
                    session_id,
                    participant,
                    private_origin,
                )
                ids = [
                    str(item)
                    for item in (candidate_ids or [])
                    if str(item or "").strip()
                ]
                if candidate_indexes:
                    candidates = [
                        item
                        for item in (meta.get("candidates") or [])
                        if isinstance(item, Mapping)
                    ]
                    if (
                        len(set(candidate_indexes)) != len(candidate_indexes)
                        or any(
                            index < 1 or index > len(candidates)
                            for index in candidate_indexes
                        )
                    ):
                        raise ValueError(
                            "候选序号无效，请重新加载角色补充后重试"
                        )
                    ids = [
                        str(candidates[index - 1].get("id") or "")
                        for index in candidate_indexes
                    ]
                    if any(not item for item in ids):
                        raise ValueError("候选资料缺失，请联系世界作者修复")
                fingerprint = request_fingerprint(
                    {
                        "session_id": session_id,
                        "participant_id": str(participant["id"]),
                        "offer_id": offer_id,
                        "action": "confirm",
                        "expected_revision": expected,
                        "candidate_ids": ids,
                        "candidate_indexes": candidate_indexes,
                        "text_value": str(text_value or "").strip(),
                    }
                )
                replay = self._supplement_action_replay_locked(
                    connection,
                    idempotency_key=request_key,
                    fingerprint=fingerprint,
                )
                if replay is not None:
                    connection.execute("COMMIT")
                    return replay
                current_revision = self._offer_revision(meta)
                if current_revision != expected:
                    raise DatabaseConflictError(
                        "supplement.revision_conflict：该角色补充已经更新或处理，"
                        "系统没有覆盖最新结果"
                    )
                if str(meta.get("state") or "") not in OFFER_OPEN_STATES:
                    raise DatabaseConflictError(
                        "该角色补充已经处理，系统没有重复执行"
                    )
                self._assert_offer_not_expired_locked(meta, turn_no, config)
                field_key = str(meta.get("field_key") or "")
                field = next(
                    (
                        item
                        for item in template.get("fields") or []
                        if isinstance(item, Mapping)
                        and str(item.get("key") or "") == field_key
                    ),
                    None,
                )
                if field is None:
                    raise ValueError("该补充字段已不在当前角色模板中")
                version_row = connection.execute(
                    """
                    SELECT * FROM character_card_versions
                    WHERE id = ?
                    """,
                    (str(participant["character_version_id"] or ""),),
                ).fetchone()
                if not version_row:
                    raise ValueError("角色卡尚未建立，无法确认补充")
                old_profile = json_load(version_row["profile_json"], {})
                if ids == ["supplement:defer"]:
                    postponed = self._postpone_locked(
                        connection,
                        session_id,
                        row,
                        meta,
                        actor=actor
                        or str(participant["private_user_id"] or ""),
                    )
                    now = utc_now()
                    new_revision = expected + 1
                    refreshed = connection.execute(
                        "SELECT meta_json FROM delivery_outbox WHERE id=?",
                        (offer_id,),
                    ).fetchone()
                    refreshed_meta = json_load(
                        refreshed["meta_json"] if refreshed else "{}",
                        {},
                    )
                    refreshed_meta["revision"] = new_revision
                    connection.execute(
                        "UPDATE delivery_outbox SET meta_json=?, updated_at=? WHERE id=?",
                        (json_dump(refreshed_meta), now, offer_id),
                    )
                    event_id, event_seq = self._append_supplement_event_locked(
                        connection,
                        session_id=session_id,
                        participant_id=str(participant["id"]),
                        turn_no=turn_no,
                        actor=str(actor or participant["private_user_id"] or "system"),
                        idempotency_key=request_key,
                        action="postpone",
                        created_at=now,
                    )
                    postponed.update(
                        {
                            "revision": new_revision,
                            "idempotent": False,
                            "event_seq": event_seq,
                        }
                    )
                    self._write_supplement_receipt_locked(
                        connection,
                        idempotency_key=request_key,
                        session_id=session_id,
                        participant_id=str(participant["id"]),
                        offer_id=offer_id,
                        action="postpone",
                        expected_revision=expected,
                        fingerprint=fingerprint,
                        event_id=event_id,
                        result=postponed,
                        created_at=now,
                    )
                    connection.execute("COMMIT")
                    return postponed
                if ids == ["supplement:reduce"]:
                    reduce_value = str(field.get("reduce_value") or "").strip()
                    if not reduce_value:
                        raise ValueError(
                            "该项暂无可用的「降低强度」版本，请回复「暂缓」稍后再试"
                        )
                    new_profile = dict(old_profile)
                    new_profile[field_key] = reduce_value
                    stage_lock_field(new_profile, field_key)
                    chosen: list[dict[str, Any]] = []
                else:
                    new_profile, chosen = apply_selection(
                        template,
                        field,
                        old_profile,
                        candidates=meta.get("candidates") or [],
                        candidate_ids=ids,
                        text_value=text_value,
                    )
                    stage_lock_field(new_profile, field_key)
                if template.get("preset_dimensions"):
                    validate_preset_selection(
                        template,
                        new_profile,
                        require_complete=False,
                    )
                    new_profile["_resolved_boundaries"] = (
                        resolve_character_presets(world, new_profile)
                    )
                preset_only_guard(template, new_profile)
                revision = validate_card_revision(world, new_profile)
                profile = revision["profile"]
                stats = revision["stats"]
                now = utc_now()
                card_row = connection.execute(
                    """
                    SELECT * FROM character_cards WHERE id = ?
                    """,
                    (str(participant["character_card_id"] or ""),),
                ).fetchone()
                if not card_row:
                    raise ValueError("角色卡不存在，无法确认补充")
                version_no = int(card_row["current_version"] or 0) + 1
                version_id = new_id("pcardv")
                connection.execute(
                    """
                    INSERT INTO character_card_versions(
                        id, character_card_id, version_no, template_version,
                        profile_json, stats_json, status, review_note,
                        reviewed_by, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'approved', ?, ?, ?)
                    """,
                    (
                        version_id,
                        str(card_row["id"]),
                        version_no,
                        int(template.get("version", 1)),
                        json_dump(profile),
                        json_dump(stats),
                        f"staged_supplement:{offer_id}",
                        str(actor or participant["private_user_id"] or "player"),
                        now,
                    ),
                )
                connection.execute(
                    """
                    UPDATE character_cards
                    SET current_version = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (version_no, now, str(card_row["id"])),
                )
                connection.execute(
                    """
                    UPDATE participants
                    SET character_version_id = ?, card_status = 'approved',
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        version_id,
                        now,
                        str(participant["id"]),
                    ),
                )
                stage = card_stage_state(template, profile)["stage"]
                self._sync_card_stage_column(
                    connection,
                    str(participant["id"]),
                    str(stage),
                )
                grants = diff_item_grant_plans(
                    card_item_grants(world, old_profile, strict=False),
                    card_item_grants(world, profile, strict=True),
                )
                if grants:
                    normalized = [
                        {
                            **dict(grant),
                            "owner_type": str(
                                grant.get("owner_scope") or "character"
                            ),
                            "owner_ref": (
                                str(participant["id"])
                                if str(
                                    grant.get("owner_scope") or "character"
                                )
                                == "character"
                                else f"party:{session_id}"
                            ),
                        }
                        for grant in grants
                    ]
                    self._grant_item_instances_locked(
                        connection,
                        session_id=session_id,
                        grants=normalized,
                        operation_id=(
                            f"card_supplement_items:{session_id}:"
                            f"{participant['id']}:{field_key}"
                        ),
                        actor_id=str(
                            actor or participant["private_user_id"] or "system"
                        ),
                        audit_action="card.supplement.items_granted",
                    )
                public_note = ""
                group_target = self._group_target_locked(
                    connection,
                    session_id,
                )
                if group_target is not None:
                    public_note = confirm_group_projection(
                        template=template,
                        field=field,
                        character_name=str(
                            participant["character_name"] or ""
                        ),
                    )
                    self._create_delivery_locked(
                        connection,
                        self._notice_record(
                            session_id=session_id,
                            target=group_target,
                            text=public_note,
                            dedupe_key=(
                                f"staged_supplement_notice:{session_id}:"
                                f"{participant['id']}:{field_key}"
                            ),
                        ),
                    )
                value_label = "，".join(
                    str(option.get("label") or option.get("value") or "")
                    for option in chosen
                )
                if not value_label:
                    value_label = str(
                        profile.get(field_key)
                        or meta.get("field_label")
                        or field_key
                    )
                new_meta = dict(meta)
                new_meta.update(
                    {
                        "state": "confirmed",
                        "revision": expected + 1,
                        "confirmed_at": now,
                        "confirmed_candidate_ids": ids,
                        "confirmed_value_label": value_label,
                        "version_id": version_id,
                    }
                )
                self._update_offer_meta_locked(
                    connection,
                    offer_id,
                    new_meta,
                    cancel_active=True,
                )
                self._insert_audit(
                    connection,
                    session_id,
                    str(actor or participant["private_user_id"] or "system"),
                    "card.supplement.confirmed",
                    offer_id,
                    {
                        "participant_id": str(participant["id"]),
                        "field_key": field_key,
                        "version_id": version_id,
                        "version_no": version_no,
                        "stage": stage,
                    },
                )
                event_id, event_seq = self._append_supplement_event_locked(
                    connection,
                    session_id=session_id,
                    participant_id=str(participant["id"]),
                    turn_no=turn_no,
                    actor=str(actor or participant["private_user_id"] or "system"),
                    idempotency_key=request_key,
                    action="confirm",
                    created_at=now,
                )
                result = {
                    "offer_id": offer_id,
                    "participant_id": str(participant["id"]),
                    "field_key": field_key,
                    "field_label": str(meta.get("field_label") or field_key),
                    "version_id": version_id,
                    "version_no": version_no,
                    "card_stage": str(stage),
                    "value_label": value_label,
                    "public_note": public_note,
                    "state": "confirmed",
                    "revision": expected + 1,
                    "idempotent": False,
                    "event_seq": event_seq,
                }
                self._write_supplement_receipt_locked(
                    connection,
                    idempotency_key=request_key,
                    session_id=session_id,
                    participant_id=str(participant["id"]),
                    offer_id=offer_id,
                    action="confirm",
                    expected_revision=expected,
                    fingerprint=fingerprint,
                    event_id=event_id,
                    result=result,
                    created_at=now,
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    # ------------------------------------------------------------------
    # 暂缓 / 拒绝 / 取消
    # ------------------------------------------------------------------

    async def postpone_supplement_offer(
        self,
        session_id: str,
        offer_id: str,
        *,
        actor: str = "",
        private_origin: str = "",
        expected_revision: int | None = None,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        return await self._run(
            self._postpone_supplement_offer,
            str(session_id or "").strip(),
            str(offer_id or "").strip(),
            str(actor or ""),
            str(private_origin or ""),
            expected_revision,
            str(idempotency_key or ""),
        )

    def _postpone_supplement_offer(
        self,
        session_id: str,
        offer_id: str,
        actor: str,
        private_origin: str,
        expected_revision: int | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        expected = require_expected_revision(expected_revision, label="角色补充版本")
        request_key = require_idempotency_key(idempotency_key)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                row = self._offer_row_locked(connection, session_id, offer_id)
                meta = json_load(row["meta_json"], {})
                participant, session, world, _template, config = (
                    self._load_offer_context_locked(
                        connection,
                        session_id,
                        row,
                        meta,
                    )
                )
                turn_no = int(session["turn_no"] or 0)
                self._assert_offer_not_expired_locked(meta, turn_no, config)
                self._assert_offer_owner_locked(
                    connection,
                    session_id,
                    participant,
                    private_origin,
                )
                fingerprint = request_fingerprint(
                    {
                        "session_id": session_id,
                        "participant_id": str(participant["id"]),
                        "offer_id": offer_id,
                        "action": "postpone",
                        "expected_revision": expected,
                    }
                )
                replay = self._supplement_action_replay_locked(
                    connection,
                    idempotency_key=request_key,
                    fingerprint=fingerprint,
                )
                if replay is not None:
                    connection.execute("COMMIT")
                    return replay
                if self._offer_revision(meta) != expected:
                    raise DatabaseConflictError(
                        "supplement.revision_conflict：该角色补充已经更新或处理，系统没有覆盖最新结果"
                    )
                if str(meta.get("state") or "") not in OFFER_OPEN_STATES:
                    raise DatabaseConflictError("该角色补充已经处理")
                result = self._postpone_locked(
                    connection,
                    session_id,
                    row,
                    meta,
                    actor=actor or str(participant["private_user_id"] or ""),
                )
                now = utc_now()
                updated_meta = json_load(
                    connection.execute(
                        "SELECT meta_json FROM delivery_outbox WHERE id=?", (offer_id,)
                    ).fetchone()["meta_json"],
                    {},
                )
                updated_meta["revision"] = expected + 1
                connection.execute(
                    "UPDATE delivery_outbox SET meta_json=?, updated_at=? WHERE id=?",
                    (json_dump(updated_meta), now, offer_id),
                )
                event_id, event_seq = self._append_supplement_event_locked(
                    connection,
                    session_id=session_id,
                    participant_id=str(participant["id"]),
                    turn_no=turn_no,
                    actor=str(actor or participant["private_user_id"] or "system"),
                    idempotency_key=request_key,
                    action="postpone",
                    created_at=now,
                )
                result.update(
                    {"revision": expected + 1, "idempotent": False, "event_seq": event_seq}
                )
                self._write_supplement_receipt_locked(
                    connection,
                    idempotency_key=request_key,
                    session_id=session_id,
                    participant_id=str(participant["id"]),
                    offer_id=offer_id,
                    action="postpone",
                    expected_revision=expected,
                    fingerprint=fingerprint,
                    event_id=event_id,
                    result=result,
                    created_at=now,
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def reject_supplement_offer(
        self,
        session_id: str,
        offer_id: str,
        *,
        candidate_ids: Sequence[str] | None = None,
        candidate_indexes: Sequence[int] | None = None,
        actor: str = "",
        private_origin: str = "",
        expected_revision: int | None = None,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        """拒绝候选项：记录去重并重新生成候选（活跃行就地更新，终态行另发提醒）。"""

        return await self._run(
            self._reject_supplement_offer,
            str(session_id or "").strip(),
            str(offer_id or "").strip(),
            [str(item) for item in (candidate_ids or [])],
            [int(item) for item in (candidate_indexes or [])],
            str(actor or ""),
            str(private_origin or ""),
            expected_revision,
            str(idempotency_key or ""),
        )
