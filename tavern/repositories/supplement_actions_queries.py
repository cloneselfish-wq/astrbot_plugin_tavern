from __future__ import annotations

from .supplement_support import *


class SupplementActionsQueriesRepositoryMixin:
    def _open_offer_locked(
        self,
        connection: Any,
        *,
        session: Any,
        participant: Any,
        template: Mapping[str, Any],
        world: Mapping[str, Any],
        profile: Mapping[str, Any],
        field: Mapping[str, Any],
        ordinal: int,
        config: Mapping[str, int],
        turn_no: int,
        trigger_source: str,
        actor: str,
    ) -> dict[str, Any] | None:
        session_id = str(session["id"])
        participant_id = str(participant["id"])
        field_key = str(field.get("key") or "")
        if not field_key:
            return None
        dedupe_key = f"staged_supplement:{session_id}:{participant_id}:{field_key}"
        now = utc_now()
        prior = connection.execute(
            """
            SELECT meta_json FROM delivery_outbox
            WHERE session_id = ? AND dedupe_key = ?
            ORDER BY created_at DESC, id DESC LIMIT 1
            """,
            (session_id, dedupe_key),
        ).fetchone()
        prior_meta = json_load(prior["meta_json"], {}) if prior else {}
        rejected = sorted(
            set(str(item) for item in prior_meta.get("rejected_ids") or [])
        )
        offer_no = int(prior_meta.get("offer_no") or 0) + 1
        bundle = build_candidates(
            template,
            field,
            profile,
            rejected_ids=rejected,
            config=config,
        )
        stored_candidates = [
            dict(option) for option in bundle["candidates"]
        ]
        views = [option_view(option) for option in stored_candidates]
        character_name = str(participant["character_name"] or "")
        text = offer_private_text(
            template=template,
            field=field,
            character_name=character_name,
            candidate_views=views,
            free_text=bool(bundle["free_text"]),
            fallback=bool(bundle["fallback"]),
        )
        meta = {
            "kind": SUPPLEMENT_KIND,
            "state": "offered",
            "revision": 1,
            "generation": 1,
            "field_key": field_key,
            "field_label": str(field.get("label") or field_key),
            "stage": field_stage(field),
            "participant_id": participant_id,
            "character_name": character_name,
            "candidates": stored_candidates,
            "rejected_ids": rejected,
            "trigger_source": trigger_source,
            "trigger_round": turn_no,
            "offer_round": turn_no,
            "expires_after_rounds": int(
                config.get("expires_after_rounds") or 6
            ),
            "reopen_after_rounds": int(
                config.get("reopen_after_rounds") or 3
            ),
            "offer_no": offer_no,
            "free_text": bool(bundle["free_text"]),
            "fallback": bool(bundle["fallback"]),
        }
        self._close_superseded_locked(connection, session_id, dedupe_key)
        target = self._private_target_for(participant)
        if target is None:
            record = self._offer_record(
                session_id=session_id,
                target=DeliveryTarget.webui_only(
                    source="staged_supplement"
                ),
                text=text,
                meta=meta,
                dedupe_key=dedupe_key,
            )
            created = self._create_delivery_locked(connection, record)
            group_target = self._group_target_locked(connection, session_id)
            if group_target is not None:
                self._create_delivery_locked(
                    connection,
                    self._notice_record(
                        session_id=session_id,
                        target=group_target,
                        text=offer_group_hint(character_name=character_name),
                        dedupe_key=(
                            f"staged_supplement_hint:{session_id}:"
                            f"{participant_id}:{field_key}"
                        ),
                    ),
                )
        else:
            record = self._offer_record(
                session_id=session_id,
                target=target,
                text=text,
                meta=meta,
                dedupe_key=dedupe_key,
            )
            created = self._create_delivery_locked(connection, record)
        self._insert_audit(
            connection,
            session_id,
            actor or str(participant["private_user_id"] or "system"),
            "card.supplement.offered",
            str(created["delivery_id"]),
            {
                "participant_id": participant_id,
                "field_key": field_key,
                "stage": str(meta["stage"]),
                "trigger_source": trigger_source,
                "trigger_round": turn_no,
                "offer_no": offer_no,
            },
        )
        return {
            "offer_id": str(created["delivery_id"]),
            "participant_id": participant_id,
            "field_key": field_key,
            "field_label": str(meta["field_label"]),
            "stage": str(meta["stage"]),
            "state": "offered",
            "candidates": views,
            "created_at": now,
            "meta": meta,
        }

    def _private_target_for(
        self,
        participant: Any,
    ) -> DeliveryTarget | None:
        private_origin = str(participant["private_origin"] or "").strip()
        private_user_id = str(participant["private_user_id"] or "").strip()
        if private_origin:
            target = DeliveryTarget.from_origin(
                private_origin,
                verified_binding=True,
                source="staged_supplement",
            )
            if target is not None:
                return target
            platform = (
                private_origin.split(":", 1)[0]
                if ":" in private_origin
                else private_origin
            )
            if platform and private_user_id:
                try:
                    return DeliveryTarget(
                        platform_instance_id=platform,
                        message_type=TARGET_KIND_PRIVATE,
                        target_id=private_user_id,
                        unified_origin=private_origin,
                        verified_binding=True,
                        source="staged_supplement",
                    )
                except ValueError:
                    return None
        return None

    def _group_target_locked(
        self,
        connection: Any,
        session_id: str,
    ) -> DeliveryTarget | None:
        session = connection.execute(
            "SELECT unified_origin FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if session is None:
            return None
        origin = str(session["unified_origin"] or "").strip()
        if not origin:
            return None
        target = DeliveryTarget.from_origin(
            origin,
            source="staged_supplement_group",
        )
        if target is None or target.message_type != TARGET_KIND_GROUP:
            return None
        return target

    def _reject_supplement_offer(
        self,
        session_id: str,
        offer_id: str,
        candidate_ids: list[str],
        candidate_indexes: list[int],
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
                participant, session, world, template, config = (
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
                if not ids:
                    raise ValueError(
                        "请回复「拒绝 序号」指明要换掉的候选项，例如：拒绝 1"
                    )
                fingerprint = request_fingerprint(
                    {
                        "session_id": session_id,
                        "participant_id": str(participant["id"]),
                        "offer_id": offer_id,
                        "action": "reject",
                        "expected_revision": expected,
                        "candidate_ids": ids,
                        "candidate_indexes": candidate_indexes,
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
                by_id = {
                    str(option.get("id") or ""): dict(option)
                    for option in meta.get("candidates") or []
                    if isinstance(option, Mapping)
                }
                unknown = [item for item in ids if item not in by_id]
                if unknown:
                    raise ValueError("所选候选项不在当前提议中")
                rejected = sorted(
                    set(str(item) for item in meta.get("rejected_ids") or [])
                    | set(ids)
                )
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
                    "SELECT profile_json FROM character_card_versions WHERE id = ?",
                    (str(participant["character_version_id"] or ""),),
                ).fetchone()
                profile = json_load(version_row["profile_json"], {}) if version_row else {}
                bundle = build_candidates(
                    template,
                    field,
                    profile,
                    rejected_ids=rejected,
                    config=field_supplement_config(field, config, world),
                )
                views = [option_view(option) for option in bundle["candidates"]]
                text = offer_private_text(
                    template=template,
                    field=field,
                    character_name=str(participant["character_name"] or ""),
                    candidate_views=views,
                    free_text=bool(bundle["free_text"]),
                    fallback=bool(bundle["fallback"]),
                )
                now = utc_now()
                current_status = str(row["status"] or "")
                if current_status in _ACTIVE_DELIVERY_STATUSES:
                    new_meta = dict(meta)
                    new_meta.update(
                        {
                            "state": "offered",
                            "revision": expected + 1,
                            "rejected_ids": rejected,
                            "candidates": [dict(item) for item in bundle["candidates"]],
                            "free_text": bool(bundle["free_text"]),
                            "fallback": bool(bundle["fallback"]),
                            "rejected_at": now,
                        }
                    )
                    connection.execute(
                        """
                        UPDATE delivery_outbox
                        SET meta_json = ?, text = ?, rendered_parts_json = ?,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            json_dump(new_meta),
                            text,
                            json_dump([text]),
                            now,
                            offer_id,
                        ),
                    )
                    delivery_id = offer_id
                else:
                    old_meta = dict(meta)
                    old_meta.update(
                        {
                            "state": "rejected",
                            "revision": expected + 1,
                            "rejected_at": now,
                        }
                    )
                    connection.execute(
                        """
                        UPDATE delivery_outbox
                        SET meta_json = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (json_dump(old_meta), now, offer_id),
                    )
                    offer_meta = dict(meta)
                    offer_meta.update(
                        {
                            "state": "offered",
                            "revision": 1,
                            "parent_offer_id": offer_id,
                            "generation": int(meta.get("generation") or 1) + 1,
                            "rejected_ids": rejected,
                            "candidates": [
                                dict(item) for item in bundle["candidates"]
                            ],
                            "free_text": bool(bundle["free_text"]),
                            "fallback": bool(bundle["fallback"]),
                            "offer_round": turn_no,
                            "trigger_source": "reject_reopen",
                            "offer_no": int(meta.get("offer_no") or 0) + 1,
                            "rejected_at": now,
                        }
                    )
                    target = self._private_target_for(participant)
                    if target is None:
                        record = self._offer_record(
                            session_id=session_id,
                            target=DeliveryTarget.webui_only(
                                source="staged_supplement"
                            ),
                            text=text,
                            meta=offer_meta,
                            dedupe_key=(
                                f"staged_supplement:{session_id}:"
                                f"{participant['id']}:{field_key}"
                            ),
                        )
                        created = self._create_delivery_locked(
                            connection,
                            record,
                        )
                        delivery_id = str(created["delivery_id"])
                        group_target = self._group_target_locked(
                            connection,
                            session_id,
                        )
                        if group_target is not None:
                            self._create_delivery_locked(
                                connection,
                                self._notice_record(
                                    session_id=session_id,
                                    target=group_target,
                                    text=offer_group_hint(
                                        character_name=str(
                                            participant["character_name"] or ""
                                        )
                                    ),
                                    dedupe_key=(
                                        f"staged_supplement_hint:{session_id}:"
                                        f"{participant['id']}:{field_key}"
                                    ),
                                ),
                            )
                    else:
                        record = self._offer_record(
                            session_id=session_id,
                            target=target,
                            text=text,
                            meta=offer_meta,
                            dedupe_key=(
                                f"staged_supplement:{session_id}:"
                                f"{participant['id']}:{field_key}"
                            ),
                        )
                        created = self._create_delivery_locked(
                            connection,
                            record,
                        )
                        delivery_id = str(created["delivery_id"])
                self._insert_audit(
                    connection,
                    session_id,
                    str(actor or participant["private_user_id"] or "system"),
                    "card.supplement.rejected",
                    offer_id,
                    {
                        "participant_id": str(participant["id"]),
                        "field_key": field_key,
                        "rejected_ids": ids,
                        "reopened_delivery_id": delivery_id,
                    },
                )
                event_id, event_seq = self._append_supplement_event_locked(
                    connection,
                    session_id=session_id,
                    participant_id=str(participant["id"]),
                    turn_no=turn_no,
                    actor=str(actor or participant["private_user_id"] or "system"),
                    idempotency_key=request_key,
                    action="reject",
                    created_at=now,
                )
                result = {
                    "offer_id": offer_id,
                    "participant_id": str(participant["id"]),
                    "field_key": field_key,
                    "rejected_ids": ids,
                    "reopened_delivery_id": delivery_id,
                    "candidate_count": len(views),
                    "state": "offered",
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
                    action="reject",
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

    async def cancel_supplement_offer(
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
            self._cancel_supplement_offer,
            str(session_id or "").strip(),
            str(offer_id or "").strip(),
            str(actor or ""),
            str(private_origin or ""),
            expected_revision,
            str(idempotency_key or ""),
        )

    def _cancel_supplement_offer(
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
                participant, session, _world, _template, _config = (
                    self._load_offer_context_locked(
                        connection,
                        session_id,
                        row,
                        meta,
                    )
                )
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
                        "action": "cancel",
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
                now = utc_now()
                new_meta = dict(meta)
                new_meta.update(
                    {
                        "state": "cancelled",
                        "revision": expected + 1,
                        "cancelled_at": now,
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
                    "card.supplement.cancelled",
                    offer_id,
                    {
                        "participant_id": str(participant["id"]),
                        "field_key": str(meta.get("field_key") or ""),
                    },
                )
                event_id, event_seq = self._append_supplement_event_locked(
                    connection,
                    session_id=session_id,
                    participant_id=str(participant["id"]),
                    turn_no=int(session["turn_no"] or 0),
                    actor=str(actor or participant["private_user_id"] or "system"),
                    idempotency_key=request_key,
                    action="cancel",
                    created_at=now,
                )
                result = {
                    "offer_id": offer_id,
                    "state": "cancelled",
                    "revision": expected + 1,
                    "idempotent": False,
                    "event_seq": event_seq,
                    "message": "已取消，本轮不再自动重开",
                }
                self._write_supplement_receipt_locked(
                    connection,
                    idempotency_key=request_key,
                    session_id=session_id,
                    participant_id=str(participant["id"]),
                    offer_id=offer_id,
                    action="cancel",
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
    # 过期（惰性）
    # ------------------------------------------------------------------

    async def expire_supplement_offers(
        self,
        session_id: str,
        *,
        turn_no: int | None = None,
        actor: str = "system",
    ) -> dict[str, Any]:
        return await self._run(
            self._expire_supplement_offers,
            str(session_id or "").strip(),
            turn_no,
            str(actor or "system"),
        )

    def _expire_supplement_offers(
        self,
        session_id: str,
        turn_no: int | None,
        actor: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not session:
                    raise DatabaseNotFoundError("副本不存在")
                if turn_no is None:
                    turn_no = int(session["turn_no"] or 0)
                config_row = connection.execute(
                    "SELECT world_snapshot_json FROM instance_configs WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                world = json_load(config_row["world_snapshot_json"], {}) if config_row else {}
                config = supplement_config(world)
                rows = connection.execute(
                    """
                    SELECT * FROM delivery_outbox
                    WHERE session_id = ? AND kind = ?
                    """,
                    (session_id, SUPPLEMENT_KIND),
                ).fetchall()
                now = utc_now()
                expired_ids: list[str] = []
                for row in rows:
                    meta = json_load(row["meta_json"], {})
                    if str(meta.get("state") or "") not in OFFER_OPEN_STATES:
                        continue
                    if not offer_expired(meta, turn_no, config):
                        continue
                    offer_id = str(row["id"])
                    new_meta = dict(meta)
                    new_meta.update(
                        {
                            "state": "expired",
                            "revision": self._offer_revision(meta) + 1,
                            "expired_at": now,
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
                        actor,
                        "card.supplement.expired",
                        offer_id,
                        {
                            "participant_id": str(
                                meta.get("participant_id") or ""
                            ),
                            "field_key": str(meta.get("field_key") or ""),
                            "turn_no": int(turn_no or 0),
                        },
                    )
                    expired_ids.append(offer_id)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return {"expired": expired_ids, "count": len(expired_ids)}

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _offer_row_locked(
        self,
        connection: Any,
        session_id: str,
        offer_id: str,
    ) -> Any:
        row = connection.execute(
            """
            SELECT * FROM delivery_outbox
            WHERE id = ? AND session_id = ?
            """,
            (str(offer_id), str(session_id)),
        ).fetchone()
        if row is None:
            raise DatabaseNotFoundError("补充提议不存在")
        meta = json_load(row["meta_json"], {})
        if str(meta.get("kind") or "") != SUPPLEMENT_KIND:
            raise ValueError("该记录不是角色补充提议")
        return row

    def _assert_offer_not_expired_locked(
        self,
        meta: Mapping[str, Any],
        turn_no: int,
        config: Mapping[str, int],
    ) -> None:
        if str(meta.get("state") or "") == "expired" or offer_expired(
            meta,
            turn_no,
            config,
        ):
            raise ValueError("该补充提议已过期，请发送「/团 当前」重新获取")

    def _assert_offer_owner_locked(
        self,
        connection: Any,
        session_id: str,
        participant: Any,
        private_origin: str,
    ) -> None:
        bound = str(participant["private_origin"] or "").strip()
        if not bound:
            raise ValueError("请先私聊绑定角色卡，再确认角色补充")
        if str(private_origin or "").strip() != bound:
            raise ValueError("只能确认属于你自己的角色补充")

    def _postpone_locked(
        self,
        connection: Any,
        session_id: str,
        row: Any,
        meta: Mapping[str, Any],
        *,
        actor: str,
    ) -> dict[str, Any]:
        if str(meta.get("state") or "") not in OFFER_OPEN_STATES:
            raise ValueError("该补充提议当前不可暂缓")
        now = utc_now()
        offer_id = str(row["id"])
        new_meta = dict(meta)
        new_meta.update({"state": "postponed", "postponed_at": now})
        self._update_offer_meta_locked(
            connection,
            offer_id,
            new_meta,
            cancel_active=True,
        )
        self._insert_audit(
            connection,
            session_id,
            actor or "system",
            "card.supplement.postponed",
            offer_id,
            {
                "participant_id": str(meta.get("participant_id") or ""),
                "field_key": str(meta.get("field_key") or ""),
            },
        )
        return {
            "offer_id": offer_id,
            "state": "postponed",
            "field_key": str(meta.get("field_key") or ""),
            "field_label": str(meta.get("field_label") or ""),
            "reopen_after_rounds": int(
                meta.get("reopen_after_rounds") or 3
            ),
            "message": "已暂缓，稍后将再次出现补充入口",
        }

    def _update_offer_meta_locked(
        self,
        connection: Any,
        offer_id: str,
        meta: Mapping[str, Any],
        *,
        cancel_active: bool,
    ) -> None:
        now = utc_now()
        if cancel_active:
            connection.execute(
                """
                UPDATE delivery_outbox
                SET status = 'cancelled', cancelled_at = ?, updated_at = ?
                WHERE id = ? AND status IN (
                    'pending', 'leased', 'partially_sent', 'retry_wait'
                )
                """,
                (now, now, str(offer_id)),
            )
        connection.execute(
            """
            UPDATE delivery_outbox
            SET meta_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (json_dump(dict(meta)), now, str(offer_id)),
        )

    def _close_superseded_locked(
        self,
        connection: Any,
        session_id: str,
        dedupe_key: str,
    ) -> None:
        now = utc_now()
        rows = connection.execute(
            """
            SELECT id, status, meta_json FROM delivery_outbox
            WHERE session_id = ? AND dedupe_key = ?
            """,
            (str(session_id), str(dedupe_key)),
        ).fetchall()
        for row in rows:
            meta = json_load(row["meta_json"], {})
            if str(meta.get("state") or "") not in OFFER_OPEN_STATES:
                continue
            meta.update(
                {
                    "state": "superseded",
                    "superseded_at": now,
                }
            )
            if str(row["status"] or "") in _ACTIVE_DELIVERY_STATUSES:
                connection.execute(
                    """
                    UPDATE delivery_outbox
                    SET status = 'cancelled', cancelled_at = ?, meta_json = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (now, json_dump(meta), now, str(row["id"])),
                )
            else:
                connection.execute(
                    """
                    UPDATE delivery_outbox
                    SET meta_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (json_dump(meta), now, str(row["id"])),
                )
