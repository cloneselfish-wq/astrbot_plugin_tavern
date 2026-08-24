from __future__ import annotations

from .characters_support import *


class CharacterReviewsRepositoryMixin:
    async def preview_card_draft(
        self,
        private_origin: str,
    ) -> dict[str, Any]:
        draft = await self.card_draft_for_private(private_origin)
        if not draft:
            raise DatabaseNotFoundError("当前私聊没有进行中的角色卡")
        return draft

    async def review_character_card(
        self,
        session_id: str,
        participant_ref: str,
        approved: bool,
        actor_id: str,
        note: str = "",
        expected_card_version: int | None = None,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        return await self._run(
            self._review_character_card,
            session_id,
            participant_ref,
            approved,
            actor_id,
            note,
            expected_card_version,
            idempotency_key,
        )

    def _review_character_card(
        self,
        session_id: str,
        participant_ref: str,
        approved: bool,
        actor_id: str,
        note: str,
        expected_card_version: int | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        expected_version = require_expected_revision(
            expected_card_version,
            label="角色卡版本",
        )
        request_key = require_idempotency_key(idempotency_key)
        cleaned_note = clean_text(note, max_chars=500)
        participant = self._get_participant(
            session_id,
            "",
            participant_ref,
            True,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                row = connection.execute(
                    """
                    SELECT pt.*, ccv.version_no AS card_version_no,
                           ccv.id AS card_version_id,
                           ccv.status AS card_version_status,
                           ccv.profile_json, ic.world_snapshot_json,
                           s.turn_no
                    FROM participants pt
                    LEFT JOIN character_card_versions ccv
                      ON ccv.id = pt.character_version_id
                    JOIN instance_configs ic ON ic.session_id = pt.session_id
                    JOIN sessions s ON s.id = pt.session_id
                    WHERE pt.id = ?
                    """,
                    (participant["id"],),
                ).fetchone()
                if not row or not row["character_version_id"]:
                    raise ValueError("该玩家尚未提交角色卡")
                action = "approve" if approved else "reject"
                fingerprint = request_fingerprint(
                    {
                        "session_id": str(session_id),
                        "participant_id": str(row["id"]),
                        "card_version_id": str(row["card_version_id"]),
                        "revision_request_id": "",
                        "action": action,
                        "note": cleaned_note,
                    }
                )
                receipt = connection.execute(
                    """
                    SELECT * FROM card_review_receipts
                    WHERE idempotency_key = ?
                    """,
                    (request_key,),
                ).fetchone()
                replay = replay_receipt(receipt, fingerprint=fingerprint)
                if replay is not None:
                    connection.execute("COMMIT")
                    return replay
                if int(row["card_version_no"] or 0) != expected_version:
                    raise DatabaseConflictError(
                        "card.version_conflict：角色卡版本已经变化，"
                        "系统没有覆盖新结果，请刷新后重新审核"
                    )
                if str(row["card_version_status"] or "") != CARD_PENDING:
                    raise DatabaseConflictError(
                        "角色卡已经处理，系统没有重复审核或改写历史结果"
                    )
                status = CARD_APPROVED if approved else CARD_REJECTED
                now = utc_now()
                item_receipt: dict[str, Any] = {}
                connection.execute(
                    """
                    UPDATE character_card_versions SET
                        status = ?, review_note = ?, reviewed_by = ?
                    WHERE id = ?
                    """,
                    (
                        status,
                        cleaned_note,
                        actor_id,
                        row["character_version_id"],
                    ),
                )
                connection.execute(
                    """
                    UPDATE participants SET
                        card_status = ?, ready = 0,
                        participation_status = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        status,
                        (
                            PARTICIPANT_ACTIVE
                            if approved
                            else PARTICIPANT_RESERVED
                        ),
                        now,
                        row["id"],
                    ),
                )
                connection.execute(
                    """
                    UPDATE timer_instances
                    SET status = 'cancelled', updated_at = ?
                    WHERE participant_id = ? AND timer_type = 'ready'
                      AND status IN ('active', 'paused')
                    """,
                    (now, row["id"]),
                )
                if approved:
                    world_snapshot = json_load(
                        row["world_snapshot_json"],
                        {},
                    )
                    profile = json_load(row["profile_json"], {})
                    plan = card_item_grants(
                        world_snapshot,
                        profile if isinstance(profile, Mapping) else {},
                        strict=True,
                    )
                    grants = []
                    for grant in plan.get("grants", []):
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
                                    else f"party:{session_id}"
                                ),
                            }
                        )
                    item_receipt = self._grant_item_instances_locked(
                        connection,
                        session_id=session_id,
                        grants=grants,
                        operation_id=(
                            f"card_start_items:{session_id}:{row['id']}"
                        ),
                        actor_id=actor_id,
                        audit_action="card.items_granted_on_approval",
                    )
                    config = connection.execute(
                        """
                        SELECT time_rules_json FROM instance_configs
                        WHERE session_id = ?
                        """,
                        (session_id,),
                    ).fetchone()
                    time_rules = normalize_time_rules(
                        json_load(
                            config["time_rules_json"] if config else "",
                            {},
                        )
                    )
                    self._create_timer(
                        connection,
                        session_id=session_id,
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
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "card.review",
                    row["id"],
                    {"approved": approved, "note": cleaned_note},
                )
                event_id = stable_event_id(request_key, "card-review")
                append_event(
                    connection,
                    session_id=session_id,
                    turn_no=int(row["turn_no"] or 0),
                    role="system",
                    actor_id=actor_id,
                    content=(
                        "角色卡审核已通过。"
                        if approved
                        else "角色卡审核已驳回。"
                    ),
                    meta={
                        "kind": "card.reviewed",
                        "visibility": "public",
                        "title": "角色卡审核",
                        "summary": (
                            "角色卡审核已通过。"
                            if approved
                            else "角色卡审核已驳回。"
                        ),
                        "affected_modules": ["character"],
                    },
                    event_id=event_id,
                    created_at=now,
                )
                updated = connection.execute(
                    "SELECT * FROM participants WHERE id = ?",
                    (row["id"],),
                ).fetchone()
                result = self._participant(updated)
                result["seeded_starter_loadout"] = [
                    f"『{grant.get('item_label') or grant.get('item_id')}』"
                    f" ×{grant.get('quantity')}"
                    for grant in (
                        item_receipt.get("granted", [])
                        if approved
                        else []
                    )
                    if isinstance(grant, Mapping)
                ]
                event_row = connection.execute(
                    """
                    SELECT seq FROM session_events
                    WHERE session_id=? AND event_id=?
                    """,
                    (session_id, event_id),
                ).fetchone()
                result.update(
                    {
                        "card_version": expected_version,
                        "review_status": status,
                        "idempotent": False,
                        "event_seq": int(event_row["seq"] if event_row else 0),
                    }
                )
                connection.execute(
                    """
                    INSERT INTO card_review_receipts(
                        idempotency_key, session_id, participant_id,
                        card_version_id, revision_request_id, action,
                        request_fingerprint, event_id, result_json, created_at
                    ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
                    """,
                    (
                        request_key,
                        session_id,
                        str(row["id"]),
                        str(row["card_version_id"]),
                        action,
                        fingerprint,
                        event_id,
                        json_dump(result),
                        now,
                    ),
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def request_card_revision(
        self,
        session_id: str,
        participant_ref: str,
        profile_patch: Mapping[str, Any],
        stats_patch: Mapping[str, Any],
        requester_id: str,
        note: str = "",
    ) -> dict[str, Any]:
        return await self._run(
            self._request_card_revision,
            session_id,
            participant_ref,
            dict(profile_patch),
            dict(stats_patch),
            requester_id,
            note,
        )

    def _request_card_revision(
        self,
        session_id: str,
        participant_ref: str,
        profile_patch: dict[str, Any],
        stats_patch: dict[str, Any],
        requester_id: str,
        note: str,
    ) -> dict[str, Any]:
        participant = self._get_participant(
            session_id, "", participant_ref, True
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                row = connection.execute(
                    """
                    SELECT pt.*, ccv.profile_json, ccv.stats_json,
                           ccv.version_no, ic.world_snapshot_json
                    FROM participants pt
                    JOIN character_card_versions ccv
                      ON ccv.id = pt.character_version_id
                    JOIN instance_configs ic ON ic.session_id = pt.session_id
                    WHERE pt.id = ?
                    """,
                    (participant["id"],),
                ).fetchone()
                if not row or not row["character_card_id"]:
                    raise ValueError("该玩家没有可修订的有效角色卡")
                pending = connection.execute(
                    """
                    SELECT id FROM card_revision_requests
                    WHERE participant_id = ? AND status = 'pending'
                    """,
                    (row["id"],),
                ).fetchone()
                if pending:
                    raise DatabaseConflictError("该角色已有待审核的修改申请")
                profile = json_load(row["profile_json"], {})
                profile = profile if isinstance(profile, dict) else {}
                profile.update(profile_patch)
                stats = json_load(row["stats_json"], {})
                stats = stats if isinstance(stats, dict) else {}
                stats.update(stats_patch)
                validated = validate_card_revision(
                    json_load(row["world_snapshot_json"], {}),
                    profile,
                    stats,
                )
                card = connection.execute(
                    "SELECT * FROM character_cards WHERE id = ?",
                    (row["character_card_id"],),
                ).fetchone()
                version_no = int(card["current_version"]) + 1
                now = utc_now()
                version_id = new_id("pcardv")
                connection.execute(
                    """
                    INSERT INTO character_card_versions(
                        id, character_card_id, version_no, template_version,
                        profile_json, stats_json, status, review_note,
                        reviewed_by, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending_review', '', '', ?)
                    """,
                    (
                        version_id,
                        row["character_card_id"],
                        version_no,
                        validated["template_version"],
                        json_dump(validated["profile"]),
                        json_dump(validated["stats"]),
                        now,
                    ),
                )
                connection.execute(
                    "UPDATE character_cards SET current_version = ?, updated_at = ? WHERE id = ?",
                    (version_no, now, row["character_card_id"]),
                )
                request_id = new_id("cardedit")
                connection.execute(
                    """
                    INSERT INTO card_revision_requests(
                        id, session_id, participant_id, character_card_id,
                        base_version_id, candidate_version_id, status,
                        request_note, review_note, requested_by,
                        reviewed_by, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, '', ?, '', ?, ?)
                    """,
                    (
                        request_id,
                        session_id,
                        row["id"],
                        row["character_card_id"],
                        row["character_version_id"],
                        version_id,
                        clean_text(note, max_chars=500),
                        requester_id,
                        now,
                        now,
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    requester_id,
                    "card.revision.request",
                    request_id,
                    {"base_version": row["version_no"], "candidate_version": version_no},
                )
                connection.execute("COMMIT")
                return {
                    "id": request_id,
                    "session_id": session_id,
                    "participant_id": row["id"],
                    "base_version_id": row["character_version_id"],
                    "candidate_version_id": version_id,
                    "candidate_version": version_no,
                    "status": "pending",
                    "created_at": now,
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def review_card_revision(
        self,
        request_id: str,
        approved: bool,
        actor_id: str,
        note: str = "",
        expected_candidate_version: int | None = None,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        return await self._run(
            self._review_card_revision,
            request_id,
            approved,
            actor_id,
            note,
            expected_candidate_version,
            idempotency_key,
        )

    def _review_card_revision(
        self,
        request_id: str,
        approved: bool,
        actor_id: str,
        note: str,
        expected_candidate_version: int | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        expected_version = require_expected_revision(
            expected_candidate_version,
            label="候选角色卡版本",
        )
        request_key = require_idempotency_key(idempotency_key)
        cleaned_note = clean_text(note, max_chars=500)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT request.*, config.world_snapshot_json,
                           candidate.version_no AS candidate_version_no,
                           candidate.status AS candidate_version_status,
                           session.turn_no
                    FROM card_revision_requests request
                    JOIN instance_configs config
                      ON config.session_id = request.session_id
                    JOIN character_card_versions candidate
                      ON candidate.id = request.candidate_version_id
                    JOIN sessions session ON session.id = request.session_id
                    WHERE request.id = ?
                    """,
                    (request_id,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("角色卡修改申请不存在")
                self._assert_session_writable(connection, row["session_id"])
                action = "approve" if approved else "reject"
                fingerprint = request_fingerprint(
                    {
                        "session_id": str(row["session_id"]),
                        "participant_id": str(row["participant_id"]),
                        "card_version_id": str(row["candidate_version_id"]),
                        "revision_request_id": str(request_id),
                        "action": action,
                        "note": cleaned_note,
                    }
                )
                receipt = connection.execute(
                    "SELECT * FROM card_review_receipts WHERE idempotency_key=?",
                    (request_key,),
                ).fetchone()
                replay = replay_receipt(receipt, fingerprint=fingerprint)
                if replay is not None:
                    connection.execute("COMMIT")
                    return replay
                if int(row["candidate_version_no"] or 0) != expected_version:
                    raise DatabaseConflictError(
                        "card.version_conflict：候选角色卡版本已经变化，"
                        "系统没有覆盖新结果"
                    )
                if (
                    str(row["status"] or "") != "pending"
                    or str(row["candidate_version_status"] or "") != CARD_PENDING
                ):
                    raise DatabaseConflictError("该修改申请已经处理")
                status = action + "d" if action == "approve" else "rejected"
                version_status = CARD_APPROVED if approved else CARD_REJECTED
                now = utc_now()
                connection.execute(
                    """
                    UPDATE character_card_versions
                    SET status = ?, review_note = ?, reviewed_by = ?
                    WHERE id = ?
                    """,
                    (
                        version_status,
                        cleaned_note,
                        actor_id,
                        row["candidate_version_id"],
                    ),
                )
                if approved:
                    candidate = connection.execute(
                        "SELECT profile_json FROM character_card_versions WHERE id = ?",
                        (row["candidate_version_id"],),
                    ).fetchone()
                    profile = json_load(candidate["profile_json"], {})
                    world_snapshot = json_load(
                        row["world_snapshot_json"], {}
                    )
                    template = card_template(world_snapshot)
                    name_definition = _field_for_semantic_role(
                        template, _ACTOR_NAME_ROLE
                    )
                    alias_definition = _field_for_semantic_role(
                        template, _ACTOR_ALIAS_ROLE
                    )
                    if name_definition is None or alias_definition is None:
                        raise ValueError("角色模板缺少姓名或代号语义字段")
                    character_name = clean_card_field(
                        profile.get(str(name_definition["key"])),
                        label=str(
                            name_definition.get("label") or "角色姓名"
                        ),
                        max_chars=12,
                    )
                    character_code = clean_card_field(
                        profile.get(str(alias_definition["key"])),
                        label=str(
                            alias_definition.get("label") or "副本代号"
                        ),
                        max_chars=12,
                    )
                    if not character_name or not character_code:
                        raise ValueError("角色姓名与副本代号不能为空")
                    connection.execute(
                        """
                        UPDATE participants SET character_version_id = ?,
                            character_name = ?, character_code = ?,
                            ready = 0, updated_at = ? WHERE id = ?
                        """,
                        (
                            row["candidate_version_id"],
                            character_name,
                            character_code,
                            now,
                            row["participant_id"],
                        ),
                    )

                    # A15：角色卡修订（含改名/改卡）批准后同步 players 表，
                    # 避免回合状态（get_turn_status 读取 players.character_name）
                    # 与行动选项（读取 participants.character_name）显示不一致。
                    connection.execute(
                        """
                        UPDATE players SET character_name = ?,
                            profile_json = ?, updated_at = ?
                        WHERE id = (
                            SELECT player_id FROM participants WHERE id = ?
                        )
                        """,
                        (
                            character_name,
                            json_dump(profile),
                            now,
                            row["participant_id"],
                        ),
                    )
                connection.execute(
                    """
                    UPDATE card_revision_requests SET status = ?,
                        review_note = ?, reviewed_by = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (status, cleaned_note, actor_id, now, request_id),
                )
                self._insert_audit(
                    connection,
                    row["session_id"],
                    actor_id,
                    "card.revision.review",
                    request_id,
                    {"approved": approved, "candidate_version_id": row["candidate_version_id"]},
                )
                event_id = stable_event_id(request_key, "card-review")
                summary = (
                    "角色卡修订审核已通过。"
                    if approved
                    else "角色卡修订审核已驳回。"
                )
                append_event(
                    connection,
                    session_id=str(row["session_id"]),
                    turn_no=int(row["turn_no"] or 0),
                    role="system",
                    actor_id=actor_id,
                    content=summary,
                    meta={
                        "kind": "card.reviewed",
                        "visibility": "public",
                        "title": "角色卡修订审核",
                        "summary": summary,
                        "affected_modules": ["character"],
                    },
                    event_id=event_id,
                    created_at=now,
                )
                event_row = connection.execute(
                    """
                    SELECT seq FROM session_events
                    WHERE session_id=? AND event_id=?
                    """,
                    (str(row["session_id"]), event_id),
                ).fetchone()
                result = {
                    "id": request_id,
                    "session_id": row["session_id"],
                    "participant_id": row["participant_id"],
                    "status": status,
                    "candidate_version_id": row["candidate_version_id"],
                    "candidate_version": expected_version,
                    "idempotent": False,
                    "event_seq": int(event_row["seq"] if event_row else 0),
                    "updated_at": now,
                }
                connection.execute(
                    """
                    INSERT INTO card_review_receipts(
                        idempotency_key, session_id, participant_id,
                        card_version_id, revision_request_id, action,
                        request_fingerprint, event_id, result_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request_key,
                        str(row["session_id"]),
                        str(row["participant_id"]),
                        str(row["candidate_version_id"]),
                        str(request_id),
                        action,
                        fingerprint,
                        event_id,
                        json_dump(result),
                        now,
                    ),
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def cancel_card_revision(
        self,
        request_id: str,
        actor_id: str,
        *,
        expected_candidate_version: int | None = None,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        return await self._run(
            self._cancel_card_revision,
            str(request_id or "").strip(),
            str(actor_id or "").strip(),
            expected_candidate_version,
            str(idempotency_key or ""),
        )

    def _cancel_card_revision(
        self,
        request_id: str,
        actor_id: str,
        expected_candidate_version: int | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        expected_version = require_expected_revision(
            expected_candidate_version,
            label="候选角色卡版本",
        )
        request_key = require_idempotency_key(idempotency_key)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT request.*, candidate.version_no AS candidate_version_no,
                           candidate.status AS candidate_version_status,
                           session.turn_no
                    FROM card_revision_requests request
                    JOIN character_card_versions candidate
                      ON candidate.id=request.candidate_version_id
                    JOIN sessions session ON session.id=request.session_id
                    WHERE request.id=?
                    """,
                    (request_id,),
                ).fetchone()
                if row is None:
                    raise DatabaseNotFoundError("角色卡修改申请不存在")
                self._assert_session_writable(connection, str(row["session_id"]))
                fingerprint = request_fingerprint(
                    {
                        "session_id": str(row["session_id"]),
                        "participant_id": str(row["participant_id"]),
                        "card_version_id": str(row["candidate_version_id"]),
                        "revision_request_id": request_id,
                        "action": "cancel",
                        "note": "",
                    }
                )
                receipt = connection.execute(
                    "SELECT * FROM card_review_receipts WHERE idempotency_key=?",
                    (request_key,),
                ).fetchone()
                replay = replay_receipt(receipt, fingerprint=fingerprint)
                if replay is not None:
                    connection.execute("COMMIT")
                    return replay
                if int(row["candidate_version_no"] or 0) != expected_version:
                    raise DatabaseConflictError(
                        "card.version_conflict：候选角色卡版本已经变化，"
                        "系统没有取消新版本"
                    )
                if (
                    str(row["status"] or "") != "pending"
                    or str(row["candidate_version_status"] or "") != CARD_PENDING
                ):
                    raise DatabaseConflictError("该修改申请已经处理")
                now = utc_now()
                connection.execute(
                    """
                    UPDATE card_revision_requests
                    SET status='cancelled', reviewed_by=?, updated_at=?
                    WHERE id=?
                    """,
                    (actor_id, now, request_id),
                )
                connection.execute(
                    """
                    UPDATE character_card_versions
                    SET status='superseded', reviewed_by=?
                    WHERE id=?
                    """,
                    (actor_id, str(row["candidate_version_id"])),
                )
                self._insert_audit(
                    connection,
                    str(row["session_id"]),
                    actor_id,
                    "card.revision.cancel",
                    request_id,
                    {"candidate_version": expected_version},
                )
                event_id = stable_event_id(request_key, "card-review")
                append_event(
                    connection,
                    session_id=str(row["session_id"]),
                    turn_no=int(row["turn_no"] or 0),
                    role="system",
                    actor_id=actor_id,
                    content="角色卡修订申请已取消。",
                    meta={
                        "kind": "card.reviewed",
                        "visibility": "public",
                        "title": "角色卡修订",
                        "summary": "角色卡修订申请已取消。",
                        "affected_modules": ["character"],
                    },
                    event_id=event_id,
                    created_at=now,
                )
                event_row = connection.execute(
                    """
                    SELECT seq FROM session_events
                    WHERE session_id=? AND event_id=?
                    """,
                    (str(row["session_id"]), event_id),
                ).fetchone()
                result = {
                    "id": request_id,
                    "session_id": str(row["session_id"]),
                    "participant_id": str(row["participant_id"]),
                    "status": "cancelled",
                    "candidate_version_id": str(row["candidate_version_id"]),
                    "candidate_version": expected_version,
                    "candidate_status": "superseded",
                    "idempotent": False,
                    "event_seq": int(event_row["seq"] if event_row else 0),
                    "updated_at": now,
                }
                connection.execute(
                    """
                    INSERT INTO card_review_receipts(
                        idempotency_key, session_id, participant_id,
                        card_version_id, revision_request_id, action,
                        request_fingerprint, event_id, result_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'cancel', ?, ?, ?, ?)
                    """,
                    (
                        request_key,
                        str(row["session_id"]),
                        str(row["participant_id"]),
                        str(row["candidate_version_id"]),
                        request_id,
                        fingerprint,
                        event_id,
                        json_dump(result),
                        now,
                    ),
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def list_card_revisions(
        self,
        session_id: str,
    ) -> list[dict[str, Any]]:
        return await self._run(self._list_card_revisions, session_id)

    def _list_card_revisions(self, session_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT rr.*, pt.display_name, pt.character_name,
                       base.version_no AS base_version,
                       candidate.version_no AS candidate_version,
                       candidate.profile_json, candidate.stats_json
                FROM card_revision_requests rr
                JOIN participants pt ON pt.id = rr.participant_id
                JOIN character_card_versions base ON base.id = rr.base_version_id
                JOIN character_card_versions candidate ON candidate.id = rr.candidate_version_id
                WHERE rr.session_id = ? ORDER BY rr.created_at DESC
                """,
                (session_id,),
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["profile"] = json_load(item.pop("profile_json"), {})
                item["stats"] = json_load(item.pop("stats_json"), {})
                result.append(item)
            return result
