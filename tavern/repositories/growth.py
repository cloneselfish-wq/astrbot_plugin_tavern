"""D1 技能成长域仓储（ability_track@1.0）。

对应 docs/D1_PLAN/17_SKILL_GROWTH_SYSTEM.md §5-§6：证据与里程碑只解锁
待确认预览；玩家确认后在同一 ``BEGIN IMMEDIATE`` 内写能力状态
（character_capabilities / actor_capability_instances）、审计、叙事事件
（events + session_events）与 resolution_receipts 回执。

成长状态零新表，持久化在 ``character_capabilities.state_json`` 的
``growth`` 子对象中（revision/level/level_name/snapshot/evidence/
milestones/pending/history），并镜像到 ``actor_capability_instances``
供 AI 能力投影使用。归档副本写入由 ``_assert_session_writable`` 拒绝。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..contracts.web_views.growth import project_growth_profile_view
from ..database_support import (
    DatabaseNotFoundError,
    json_dump,
    json_load,
    new_id,
    utc_now,
)
from ..lifecycle import card_template
from ..resolution_receipts import content_hash
from ..runtime.growth_service import (
    GrowthError,
    apply_upgrade,
    build_pending,
    declared_impacts,
    find_ability_track,
    growth_policy,
    level_by_number,
    list_ability_tracks,
    normalize_growth,
    position_label,
    threshold_issues,
    track_maximum_level,
    validate_confirm,
)
from .events import append_event


_PRIVILEGED_ROLES = frozenset({"dm", "admin"})


class GrowthRepositoryMixin:
    # ------------------------------------------------------------------
    # 列表 / 私聊上下文
    # ------------------------------------------------------------------

    async def list_growth_profiles(
        self,
        session_id: str,
        participant_id: str = "",
        *,
        viewer_role: str = "player",
        include_technical_refs: bool = False,
    ) -> dict[str, Any]:
        """列出成长面板；普通玩家只能看到自己的角色。"""

        return await self._run(
            self._list_growth_profiles,
            str(session_id or "").strip(),
            str(participant_id or "").strip(),
            str(viewer_role or "player"),
            bool(include_technical_refs),
        )

    def _list_growth_profiles(
        self,
        session_id: str,
        participant_id: str,
        viewer_role: str,
        include_technical_refs: bool,
    ) -> dict[str, Any]:
        if not session_id:
            return {"participant_id": participant_id, "tracks": []}
        role = str(viewer_role or "player").strip()
        privileged = role in _PRIVILEGED_ROLES
        with self._connect() as connection:
            session = connection.execute(
                "SELECT * FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if not session:
                raise DatabaseNotFoundError("副本不存在")
            config_row = connection.execute(
                """
                SELECT world_snapshot_json FROM instance_configs
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            world = (
                json_load(config_row["world_snapshot_json"], {})
                if config_row
                else {}
            )
            policy = growth_policy(world)
            tracks = list_ability_tracks(world)
            if not policy.get("enabled") or not tracks:
                return {"participant_id": participant_id, "tracks": []}
            labels = self._profession_labels(world)
            if privileged and not participant_id:
                rows = connection.execute(
                    """
                    SELECT * FROM participants
                    WHERE session_id = ?
                      AND card_status = 'approved'
                      AND participation_status NOT IN ('retired', 'archived')
                    ORDER BY updated_at DESC, id ASC
                    """,
                    (session_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM participants
                    WHERE session_id = ? AND id = ?
                    """,
                    (session_id, participant_id),
                ).fetchall()
            profiles: list[dict[str, Any]] = []
            for row in rows:
                participant = dict(row)
                character_id = str(participant["id"] or "")
                character_name = str(
                    participant["character_name"]
                    or participant["display_name"]
                    or "角色"
                )
                for track in tracks:
                    capability_ref = str(track.get("signature_ability_ref") or "")
                    capability_row = connection.execute(
                        """
                        SELECT * FROM character_capabilities
                        WHERE session_id = ? AND character_id = ?
                          AND capability_ref = ? AND available = 1
                        """,
                        (session_id, character_id, capability_ref),
                    ).fetchone()
                    if capability_row is None:
                        continue
                    profile = self._profile_view_locked(
                        connection,
                        session_id=session_id,
                        participant=participant,
                        world=world,
                        policy=policy,
                        track=track,
                        capability_row=capability_row,
                        labels=labels,
                        include_technical_refs=include_technical_refs,
                    )
                    if profile is not None:
                        profiles.append(profile)
        return {
            "participant_id": participant_id,
            "tracks": profiles,
        }

    async def growth_context_for_private(
        self,
        private_origin: str,
    ) -> dict[str, Any] | None:
        """按真实私聊来源解析参与者与本人成长面板。"""

        context = await self._run(
            self._growth_context_for_private,
            str(private_origin or "").strip(),
        )
        if context is None:
            return None
        profiles = await self.list_growth_profiles(
            str(context["session_id"]),
            participant_id=str(context["participant_id"]),
            viewer_role="player",
        )
        return {**context, "profiles": profiles}

    def _growth_context_for_private(
        self,
        private_origin: str,
    ) -> dict[str, Any] | None:
        if not private_origin:
            return None
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT pt.id AS participant_id, pt.session_id,
                       pt.private_user_id, pt.character_name,
                       pt.display_name, pt.card_stage, s.turn_no, s.state
                FROM participants pt
                JOIN sessions s ON s.id = pt.session_id
                WHERE pt.private_origin = ?
                  AND pt.card_status = 'approved'
                  AND pt.participation_status NOT IN ('retired', 'archived')
                ORDER BY pt.updated_at DESC, pt.id DESC
                LIMIT 1
                """,
                (private_origin,),
            ).fetchone()
        if row is None:
            return None
        return {
            "session_id": str(row["session_id"] or ""),
            "participant_id": str(row["participant_id"] or ""),
            "private_user_id": str(row["private_user_id"] or ""),
            "character_name": str(
                row["character_name"] or row["display_name"] or "角色"
            ),
            "card_stage": str(row["card_stage"] or ""),
            "turn_no": int(row["turn_no"] or 0),
            "session_state": str(row["state"] or ""),
        }

    # ------------------------------------------------------------------
    # 成长证据 / 里程碑
    # ------------------------------------------------------------------

    async def record_growth_evidence(
        self,
        session_id: str,
        participant_id: str,
        track_ref: str,
        *,
        evidence_id: str = "",
        kind: str = "",
        note: str = "",
        actor: str = "",
        milestone: bool = False,
    ) -> dict[str, Any]:
        """记录一次成长证据（或里程碑）；达到阈值时生成待确认预览。"""

        return await self._run(
            self._record_growth_evidence,
            str(session_id or "").strip(),
            str(participant_id or "").strip(),
            str(track_ref or "").strip(),
            str(evidence_id or "").strip(),
            str(kind or "").strip(),
            str(note or "").strip(),
            str(actor or "").strip(),
            bool(milestone),
        )

    def _record_growth_evidence(
        self,
        session_id: str,
        participant_id: str,
        track_ref: str,
        evidence_id: str,
        kind: str,
        note: str,
        actor: str,
        milestone: bool,
    ) -> dict[str, Any]:
        result_view: dict[str, Any] = {}
        pending_created = False
        pending: dict[str, Any] | None = None
        message = "已记录成长证据。"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                participant, session, world, policy = (
                    self._load_growth_context_locked(
                        connection,
                        session_id,
                        participant_id,
                    )
                )
                if not policy.get("enabled"):
                    raise GrowthError(
                        "本世界未启用技能成长，无法记录成长证据。",
                        code="growth.disabled",
                    )
                track = self._require_track(world, track_ref)
                capability_row, _actor_row = self._require_capability_rows(
                    connection,
                    session_id,
                    str(participant["id"] or ""),
                    str(track.get("signature_ability_ref") or ""),
                )
                growth = normalize_growth(
                    json_load(capability_row["state_json"], {}),
                    track,
                )
                resolved_id = evidence_id or new_id("growth_evidence")
                store = (
                    growth["milestones"]
                    if milestone
                    else growth["evidence"]
                )
                for item in store:
                    if str(item.get("evidence_id") or "") == resolved_id:
                        result_view = self._profile_view_locked(
                            connection,
                            session_id=session_id,
                            participant=participant,
                            world=world,
                            policy=policy,
                            track=track,
                            capability_row=capability_row,
                            labels=self._profession_labels(world),
                        )
                        connection.execute("COMMIT")
                        return {
                            "status": "already_recorded",
                            "pending_created": False,
                            "message": "该成长记录已存在，无需重复提交。",
                            "view": result_view,
                        }
                now = utc_now()
                record = {
                    "evidence_id": resolved_id,
                    "kind": str(kind or "")[:80],
                    "note": str(note or "")[:300],
                    "actor": str(actor or "")[:120],
                    "recorded_at": now,
                }
                store.append(record)
                had_pending = growth.get("pending") is not None
                new_pending = build_pending(growth, track, policy, now)
                if new_pending is not None:
                    growth["pending"] = new_pending
                    pending = dict(new_pending)
                    pending_created = not had_pending
                self._persist_growth_locked(
                    connection,
                    session_id,
                    str(participant["id"] or ""),
                    str(track.get("signature_ability_ref") or ""),
                    growth,
                    now,
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor or "system",
                    (
                        "growth.milestone.recorded"
                        if milestone
                        else "growth.evidence.recorded"
                    ),
                    str(track.get("track_id") or track_ref),
                    {
                        "participant_id": str(participant["id"] or ""),
                        "evidence_id": resolved_id,
                        "kind": str(kind or "")[:80],
                    },
                )
                if pending_created and new_pending is not None:
                    current_name = str(growth.get("level_name") or "")
                    target_name = str(new_pending.get("target_name") or "")
                    append_event(
                        connection,
                        session_id=session_id,
                        turn_no=int(session["turn_no"] or 0),
                        role="system",
                        actor_id=actor or "system",
                        actor_name="开团系统",
                        content=(
                            f"「{current_name}」已满足升级候选条件，"
                            f"可确认升级为「{target_name}」。"
                        ),
                        event_id=f"event:growth.pending:{resolved_id}",
                        meta={
                            "kind": "capability.growth",
                            "title": "能力可升级",
                            "summary": (
                                f"「{current_name}」已满足升级候选条件，"
                                f"等待确认升级为「{target_name}」。"
                            ),
                            "affected_modules": ["progression"],
                            "visibility": "public",
                        },
                    )
                result_view = self._profile_view_locked(
                    connection,
                    session_id=session_id,
                    participant=participant,
                    world=world,
                    policy=policy,
                    track=track,
                    capability_row=capability_row,
                    labels=self._profession_labels(world),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return {
            "status": "recorded",
            "pending_created": pending_created,
            "pending": pending,
            "message": "已记录里程碑。" if milestone else message,
            "view": result_view,
        }

    # ------------------------------------------------------------------
    # 确认升级
    # ------------------------------------------------------------------

    async def confirm_growth(
        self,
        session_id: str,
        participant_id: str,
        track_ref: str,
        *,
        actor: str = "",
        private_origin: str = "",
        operation_id: str = "",
        authority_confirm: bool = False,
    ) -> dict[str, Any]:
        """玩家确认一次升级：同事务写能力状态、审计、事件与回执。"""

        return await self._run(
            self._confirm_growth,
            str(session_id or "").strip(),
            str(participant_id or "").strip(),
            str(track_ref or "").strip(),
            str(actor or "").strip(),
            str(private_origin or "").strip(),
            str(operation_id or "").strip(),
            bool(authority_confirm),
        )

    def _confirm_growth(
        self,
        session_id: str,
        participant_id: str,
        track_ref: str,
        actor: str,
        private_origin: str,
        operation_id: str,
        authority_confirm: bool,
    ) -> dict[str, Any]:
        result_view: dict[str, Any] = {}
        resolved_operation = ""
        receipt_id = ""
        old_name = ""
        new_name = ""
        message = ""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                participant, session, world, policy = (
                    self._load_growth_context_locked(
                        connection,
                        session_id,
                        participant_id,
                    )
                )
                if not policy.get("enabled"):
                    raise GrowthError(
                        "本世界未启用技能成长，无法确认升级。",
                        code="growth.disabled",
                    )
                bound = str(participant["private_origin"] or "").strip()
                if private_origin and private_origin != bound:
                    raise GrowthError(
                        "只能确认属于你自己的技能成长。",
                        code="growth.not_owner",
                    )
                track = self._require_track(world, track_ref)
                capability_row, _actor_row = self._require_capability_rows(
                    connection,
                    session_id,
                    str(participant["id"] or ""),
                    str(track.get("signature_ability_ref") or ""),
                )
                growth = normalize_growth(
                    json_load(capability_row["state_json"], {}),
                    track,
                )
                pending = (
                    dict(growth["pending"])
                    if isinstance(growth.get("pending"), Mapping)
                    else None
                )
                resolved_operation = (
                    operation_id
                    or self._growth_operation_id(
                        session_id,
                        str(participant["id"] or ""),
                        str(track.get("track_id") or track_ref),
                        int((pending or {}).get("target_level") or 0),
                    )
                )
                existing_receipt = connection.execute(
                    """
                    SELECT * FROM resolution_receipts
                    WHERE operation_id = ?
                    """,
                    (resolved_operation,),
                ).fetchone()
                if existing_receipt is not None:
                    result_view = self._profile_view_locked(
                        connection,
                        session_id=session_id,
                        participant=participant,
                        world=world,
                        policy=policy,
                        track=track,
                        capability_row=capability_row,
                        labels=self._profession_labels(world),
                    )
                    connection.execute("COMMIT")
                    return {
                        "status": "already_confirmed",
                        "message": "该升级已经确认完成，无需重复确认。",
                        "operation_id": resolved_operation,
                        "view": result_view,
                    }
                validate_confirm(
                    growth,
                    track,
                    policy,
                    pending,
                    authority_confirm=authority_confirm,
                )
                target = int((pending or {}).get("target_level") or 0)
                resolved_operation = (
                    operation_id
                    or self._growth_operation_id(
                        session_id,
                        str(participant["id"] or ""),
                        str(track.get("track_id") or track_ref),
                        target,
                    )
                )
                now = utc_now()
                old_name = str(growth.get("level_name") or "")
                new_growth = apply_upgrade(
                    growth,
                    track,
                    policy,
                    pending,
                    operation_id=resolved_operation,
                    confirmed_at=now,
                )
                new_name = str(new_growth.get("level_name") or "")
                self._persist_growth_locked(
                    connection,
                    session_id,
                    str(participant["id"] or ""),
                    str(track.get("signature_ability_ref") or ""),
                    new_growth,
                    now,
                )
                append_event(
                    connection,
                    session_id=session_id,
                    turn_no=int(session["turn_no"] or 0),
                    role="system",
                    actor_id=actor or "system",
                    actor_name="开团系统",
                    content=f"「{old_name}」升级为「{new_name}」。",
                    event_id=(
                        f"event:growth:{session_id}:{participant_id}:"
                        f"{track_ref}:{target}"
                    ),
                    meta={
                        "kind": "capability.growth",
                        "title": "技能升级",
                        "summary": (
                            f"「{old_name}」升级为「{new_name}」，"
                            "技能名称、效果与限制已更新。"
                        ),
                        "affected_modules": ["progression"],
                        "visibility": "public",
                    },
                )
                evidence_count, milestone_count = self._growth_counts(
                    new_growth
                )
                receipt_payload = {
                    "kind": "ability_track.growth",
                    "session_id": session_id,
                    "participant_id": str(participant["id"] or ""),
                    "track_ref": str(track.get("track_id") or track_ref),
                    "from_level": int(growth.get("level") or 0),
                    "to_level": target,
                    "from_name": old_name,
                    "to_name": new_name,
                    "revision": int(new_growth.get("revision") or 0),
                    "evidence_count": evidence_count,
                    "milestone_count": milestone_count,
                    "authority_confirm": bool(authority_confirm),
                    "created_at": now,
                }
                receipt_id = new_id("receipt")
                connection.execute(
                    """
                    INSERT INTO resolution_receipts(
                        receipt_id, operation_id, session_id,
                        world_snapshot_id, content_hash, receipt_json,
                        public_projection_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        receipt_id,
                        resolved_operation,
                        session_id,
                        str(session["world_id"] or ""),
                        content_hash(receipt_payload),
                        json_dump(receipt_payload),
                        json_dump(
                            {
                                "to_name": new_name,
                                "from_name": old_name,
                                "level": target,
                                "message": (
                                    f"「{old_name}」已升级为「{new_name}」。"
                                ),
                            }
                        ),
                        now,
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor or str(participant["private_user_id"] or "system"),
                    "growth.confirmed",
                    str(track.get("track_id") or track_ref),
                    {
                        "participant_id": str(participant["id"] or ""),
                        "operation_id": resolved_operation,
                        "receipt_id": receipt_id,
                        "from_level": int(growth.get("level") or 0),
                        "to_level": target,
                        "from_name": old_name,
                        "to_name": new_name,
                        "authority_confirm": bool(authority_confirm),
                    },
                )
                result_view = self._profile_view_locked(
                    connection,
                    session_id=session_id,
                    participant=participant,
                    world=world,
                    policy=policy,
                    track=track,
                    capability_row=capability_row,
                    labels=self._profession_labels(world),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return {
            "status": "confirmed",
            "message": message or f"「{old_name}」已升级为「{new_name}」。",
            "operation_id": resolved_operation,
            "receipt_id": receipt_id,
            "view": result_view,
        }

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _growth_operation_id(
        session_id: str,
        participant_id: str,
        track_ref: str,
        target_level: int,
    ) -> str:
        return (
            f"growth:{session_id}:{participant_id}:"
            f"{track_ref}:{int(target_level or 0)}"
        )

    @staticmethod
    def _growth_counts(growth: Mapping[str, Any]) -> tuple[int, int]:
        evidence = [
            item
            for item in growth.get("evidence") or []
            if isinstance(item, Mapping)
        ]
        milestones = [
            item
            for item in growth.get("milestones") or []
            if isinstance(item, Mapping)
        ]
        return len(evidence), len(milestones)

    @staticmethod
    def _require_track(
        world: Mapping[str, Any],
        track_ref: str,
    ) -> dict[str, Any]:
        track = find_ability_track(world, track_ref)
        if track is None:
            raise GrowthError(
                "世界未声明该技能轨迹，无法继续。",
                code="growth.unknown_track",
            )
        return track

    @staticmethod
    def _require_capability_rows(
        connection: Any,
        session_id: str,
        character_id: str,
        capability_ref: str,
    ) -> tuple[Any, Any | None]:
        capability_row = connection.execute(
            """
            SELECT * FROM character_capabilities
            WHERE session_id = ? AND character_id = ?
              AND capability_ref = ? AND available = 1
            """,
            (session_id, character_id, capability_ref),
        ).fetchone()
        if capability_row is None:
            raise GrowthError(
                "角色尚未获得该能力，无法升级。",
                code="growth.capability_missing",
            )
        actor_row = connection.execute(
            """
            SELECT * FROM actor_capability_instances
            WHERE session_id = ? AND actor_ref = ?
              AND capability_ref = ?
            """,
            (session_id, f"character:{character_id}", capability_ref),
        ).fetchone()
        return capability_row, actor_row

    @staticmethod
    def _profession_labels(
        world: Mapping[str, Any],
    ) -> tuple[dict[str, str], dict[str, str]]:
        """从角色卡模板读取职业/专精名称；缺失时安全回退为空。"""

        profession_labels: dict[str, str] = {}
        specialization_labels: dict[str, str] = {}
        try:
            template = card_template(world)
            presets = template.get("preset_sets") or {}
            if isinstance(presets, Mapping):
                for preset in presets.get("profession_presets") or []:
                    if not isinstance(preset, Mapping):
                        continue
                    preset_id = str(preset.get("id") or "").strip()
                    label = str(
                        preset.get("name") or preset.get("label") or ""
                    ).strip()
                    if preset_id and label:
                        profession_labels[preset_id] = label
                    for option in preset.get("specialization_options") or []:
                        if not isinstance(option, Mapping):
                            continue
                        option_id = str(option.get("id") or "").strip()
                        option_label = str(
                            option.get("label") or option.get("name") or ""
                        ).strip()
                        if option_id and option_label:
                            specialization_labels[option_id] = option_label
        except (KeyError, TypeError, ValueError):
            return {}, {}
        return profession_labels, specialization_labels

    def _load_growth_context_locked(
        self,
        connection: Any,
        session_id: str,
        participant_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
        session = connection.execute(
            "SELECT * FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if session is None:
            raise DatabaseNotFoundError("副本不存在")
        participant = connection.execute(
            "SELECT * FROM participants WHERE id = ? AND session_id = ?",
            (participant_id, session_id),
        ).fetchone()
        if participant is None:
            raise DatabaseNotFoundError("角色不存在")
        config_row = connection.execute(
            """
            SELECT world_snapshot_json FROM instance_configs
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        world = (
            json_load(config_row["world_snapshot_json"], {})
            if config_row
            else {}
        )
        return (
            dict(participant),
            dict(session),
            world,
            growth_policy(world),
        )

    def _persist_growth_locked(
        self,
        connection: Any,
        session_id: str,
        character_id: str,
        capability_ref: str,
        growth: Mapping[str, Any],
        now: str,
    ) -> None:
        character_row = connection.execute(
            """
            SELECT state_json FROM character_capabilities
            WHERE session_id = ? AND character_id = ?
              AND capability_ref = ?
            """,
            (session_id, character_id, capability_ref),
        ).fetchone()
        if character_row is None:
            raise GrowthError(
                "角色能力记录不存在，无法写入成长状态。",
                code="growth.capability_missing",
            )
        state = dict(json_load(character_row["state_json"], {}))
        state["growth"] = dict(growth)
        connection.execute(
            """
            UPDATE character_capabilities
            SET state_json = ?, updated_at = ?
            WHERE session_id = ? AND character_id = ?
              AND capability_ref = ?
            """,
            (
                json_dump(state),
                now,
                session_id,
                character_id,
                capability_ref,
            ),
        )
        actor_row = connection.execute(
            """
            SELECT state_json FROM actor_capability_instances
            WHERE session_id = ? AND actor_ref = ?
              AND capability_ref = ?
            """,
            (session_id, f"character:{character_id}", capability_ref),
        ).fetchone()
        if actor_row is not None:
            actor_state = dict(json_load(actor_row["state_json"], {}))
            actor_state["growth"] = dict(growth)
            connection.execute(
                """
                UPDATE actor_capability_instances
                SET state_json = ?, updated_at = ?
                WHERE session_id = ? AND actor_ref = ?
                  AND capability_ref = ?
                """,
                (
                    json_dump(actor_state),
                    now,
                    session_id,
                    f"character:{character_id}",
                    capability_ref,
                ),
            )

    def _profile_view_locked(
        self,
        connection: Any,
        *,
        session_id: str,
        participant: Mapping[str, Any],
        world: Mapping[str, Any],
        policy: Mapping[str, Any],
        track: Mapping[str, Any],
        capability_row: Any,
        labels: tuple[dict[str, str], dict[str, str]],
        include_technical_refs: bool = False,
    ) -> dict[str, Any] | None:
        capability_row = connection.execute(
            """
            SELECT * FROM character_capabilities
            WHERE session_id = ? AND character_id = ?
              AND capability_ref = ?
            """,
            (
                session_id,
                str(participant.get("id") or ""),
                str(track.get("signature_ability_ref") or ""),
            ),
        ).fetchone()
        if capability_row is None:
            return None
        growth = normalize_growth(
            json_load(capability_row["state_json"], {}),
            track,
        )
        profession_labels, specialization_labels = labels
        profession_ref = str(track.get("profession_ref") or "")
        specialization_ref = str(track.get("specialization_ref") or "")
        current_level = int(growth.get("level") or 0)
        maximum = track_maximum_level(track, policy)
        unmet = list(threshold_issues(growth, policy))
        pending = growth.get("pending")
        pending = dict(pending) if isinstance(pending, Mapping) else None
        if pending is not None and str(pending.get("state") or "") != "preview":
            pending = None
        target_level_data = None
        if pending is not None:
            target_level_data = level_by_number(
                track,
                int(pending.get("target_level") or 0),
            )
        impacts = (
            declared_impacts(target_level_data)
            if target_level_data is not None
            else []
        )
        snapshot = dict(growth.get("snapshot") or {})
        view = project_growth_profile_view(
            character_name=str(
                participant.get("character_name")
                or participant.get("display_name")
                or "角色"
            ),
            capability_name=str(growth.get("level_name") or ""),
            level=current_level,
            position_label=position_label(current_level),
            source_profession=profession_labels.get(
                profession_ref,
                "",
            ),
            source_specialization=specialization_labels.get(
                specialization_ref,
                "",
            ),
            summary=str(snapshot.get("summary") or ""),
            effects=snapshot.get("effects") or [],
            costs=snapshot.get("costs") or [],
            limitations=snapshot.get("limitations") or [],
            evidence=growth.get("evidence") or [],
            milestones=growth.get("milestones") or [],
            history=growth.get("history") or [],
            pending=pending,
            unmet_conditions=unmet if current_level < maximum else [],
            maximum_level=maximum,
            impacts=impacts,
            include_technical_refs=include_technical_refs,
            technical={
                "track_ref": str(track.get("track_id") or ""),
                "capability_ref": str(
                    track.get("signature_ability_ref") or ""
                ),
                "participant_ref": str(participant.get("id") or ""),
                "revision": int(growth.get("revision") or 0),
            }
            if include_technical_refs
            else None,
        )
        return view


__all__ = ["GrowthRepositoryMixin"]
