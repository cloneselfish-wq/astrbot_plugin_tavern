from .common import *
from ..recovery_ranges import parse_recovery_json
from ..contracts.narrative_document import (
    legacy_text_fallback,
    narrative_document_to_plain_text,
    narrative_text_sha256,
    parse_narrative_document,
    project_public_narrative_document,
)

class EventProjectionMixin:

    # ── D1 Schema 20：副本事件与增量投影 ───────────────────────────────

    async def append_session_event(
        self,
        *,
        session_id: str,
        event_id: str,
        type_: str,
        actor_ref: str = "",
        command_id: str = "",
        causation_id: str = "",
        correlation_id: str = "",
        payload: Mapping[str, Any] | None = None,
        visibility: str = "public",
        created_at: str | None = None,
    ) -> dict[str, Any]:
        """集中写入副本领域事件（按 event_id 幂等，D1-RUN-006）。"""
        return await self._run(
            self._append_session_event,
            session_id,
            event_id,
            type_,
            actor_ref,
            command_id,
            causation_id,
            correlation_id,
            payload,
            visibility,
            created_at,
        )

    def _append_session_event(
        self,
        session_id: str,
        event_id: str,
        type_: str,
        actor_ref: str,
        command_id: str,
        causation_id: str,
        correlation_id: str,
        payload: Mapping[str, Any] | None,
        visibility: str,
        created_at: str | None,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                result = insert_session_event(
                    connection,
                    session_id=session_id,
                    event_id=event_id,
                    type_=type_,
                    actor_ref=actor_ref,
                    command_id=command_id,
                    causation_id=causation_id,
                    correlation_id=correlation_id,
                    payload=payload,
                    visibility=visibility,
                    created_at=created_at,
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return result

    async def list_session_events(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        limit: int = 200,
        visibility: str | Sequence[str] = "",
    ) -> list[dict[str, Any]]:
        """增量事件读取：after_seq 之后的按 seq 升序事件列表。"""
        return await self._run(
            self._list_session_events,
            session_id,
            after_seq,
            limit,
            visibility,
        )

    def _list_session_events(
        self,
        session_id: str,
        after_seq: int,
        limit: int,
        visibility: str | Sequence[str],
    ) -> list[dict[str, Any]]:
        clauses = ["session_events.session_id = ?", "session_events.seq > ?"]
        values: list[Any] = [str(session_id), max(0, int(after_seq or 0))]
        if isinstance(visibility, Sequence) and not isinstance(visibility, (str, bytes)):
            visible = tuple(dict.fromkeys(str(item) for item in visibility if str(item)))
            if visible:
                placeholders = ",".join("?" for _ in visible)
                clauses.append(f"session_events.visibility IN ({placeholders})")
                values.extend(visible)
        elif visibility:
            clauses.append("session_events.visibility = ?")
            values.append(str(visibility))
        with self._connect() as connection:
            recovery_row = connection.execute(
                "SELECT recovery_json FROM session_rule_states "
                "WHERE session_id = ?",
                (str(session_id),),
            ).fetchone()
            recovery = parse_recovery_json(
                recovery_row["recovery_json"] if recovery_row else "{}"
            )
            for start, end in recovery.excluded_event_ranges:
                clauses.append(
                    "(events.seq IS NULL OR "
                    "NOT (events.seq BETWEEN ? AND ?))"
                )
                values.extend((start, end))
            values.append(max(1, min(1000, int(limit or 200))))
            rows = connection.execute(
                f"""
                SELECT session_events.*,
                       story_documents.document_json AS story_document_json,
                       story_documents.plain_text AS story_plain_text,
                       story_documents.text_sha256 AS story_text_sha256,
                       events.meta_json AS story_event_meta_json,
                       events.content AS story_event_content,
                       events.role AS story_event_role
                FROM session_events
                LEFT JOIN story_documents
                  ON story_documents.event_id = session_events.event_id
                LEFT JOIN events
                  ON events.id = session_events.event_id
                WHERE {' AND '.join(clauses)}
                ORDER BY seq ASC LIMIT ?
                """,
                tuple(values),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            document_json = item.pop("story_document_json", None)
            plain_text = item.pop("story_plain_text", None)
            text_sha256 = item.pop("story_text_sha256", None)
            event_meta = json_load(
                item.pop("story_event_meta_json", None), {}
            )
            event_content = item.pop("story_event_content", None)
            event_role = str(item.pop("story_event_role", None) or "")
            if document_json is not None:
                try:
                    document = parse_narrative_document(
                        json_load(document_json, {}),
                        dialogue_expected=False,
                    )
                    if (
                        narrative_document_to_plain_text(document)
                        != str(plain_text or "")
                        or narrative_text_sha256(document)
                        != str(text_sha256 or "")
                    ):
                        raise ValueError("story document integrity mismatch")
                    item["narrative_document"] = (
                        project_public_narrative_document(document)
                    )
                except Exception:
                    item["narrative_document"] = None
                    item["narrative_problem"] = {
                        "code": "history.story_document_corrupt",
                        "message": "这段故事未通过完整性检查。",
                        "recovery": "请由主持人从已验证备份恢复该记录。",
                        "retryable": False,
                    }
            elif str(item.get("type") or "") == "event:story_progress":
                explicit_legacy = bool(
                    event_role == "narrator"
                    and isinstance(event_meta, Mapping)
                    and event_meta.get("legacy_record") is True
                )
                if explicit_legacy:
                    try:
                        legacy = legacy_text_fallback(
                            event_content, legacy_record=True
                        )
                        item.update(legacy.to_dict())
                        item["narrative_document"] = None
                    except Exception:
                        item["narrative_document"] = None
                        item["narrative_problem"] = {
                            "code": "history.legacy_record_invalid",
                            "message": "明确标记的旧故事正文未通过安全检查。",
                            "recovery": "请由主持人从已验证的旧版备份恢复该记录。",
                            "retryable": False,
                        }
                else:
                    item["narrative_document"] = None
                    item["narrative_problem"] = {
                        "code": "history.story_document_missing",
                        "message": "这段故事缺少结构化正文。",
                        "recovery": "请由主持人检查最近提交或恢复已验证备份。",
                        "retryable": True,
                    }
            result.append(item)
        return result

    async def latest_session_event_seq(self, session_id: str) -> int:
        """当前副本最高事件序号（空副本为 0）。"""
        return await self._run(self._latest_session_event_seq, session_id)

    def _latest_session_event_seq(self, session_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COALESCE(MAX(seq), 0) AS latest
                FROM session_events WHERE session_id = ?
                """,
                (str(session_id),),
            ).fetchone()
        return int(row["latest"] if row is not None else 0)

    async def set_projection_checkpoint(
        self,
        *,
        session_id: str,
        projection_name: str,
        last_seq: int,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """写入/推进投影检查点（WP-11 断线恢复锚点）。"""
        return await self._run(
            self._set_projection_checkpoint,
            session_id,
            projection_name,
            last_seq,
            payload,
        )

    def _set_projection_checkpoint(
        self,
        session_id: str,
        projection_name: str,
        last_seq: int,
        payload: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                connection.execute(
                    """
                    INSERT INTO projection_checkpoints(
                        session_id, projection_name, last_seq,
                        payload_json, revision, updated_at
                    ) VALUES (?, ?, ?, ?, 1, ?)
                    ON CONFLICT(session_id, projection_name) DO UPDATE SET
                        last_seq = excluded.last_seq,
                        payload_json = excluded.payload_json,
                        revision = projection_checkpoints.revision + 1,
                        updated_at = excluded.updated_at
                    """,
                    (
                        str(session_id),
                        str(projection_name)[:120],
                        max(0, int(last_seq or 0)),
                        json_dump(dict(payload) if payload else {}),
                        now,
                    ),
                )
                row = connection.execute(
                    """
                    SELECT * FROM projection_checkpoints
                    WHERE session_id = ? AND projection_name = ?
                    """,
                    (str(session_id), str(projection_name)[:120]),
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return dict(row)

    async def get_projection_checkpoint(
        self,
        session_id: str,
        projection_name: str,
    ) -> dict[str, Any] | None:
        return await self._run(
            self._get_projection_checkpoint,
            session_id,
            projection_name,
        )

    def _get_projection_checkpoint(
        self,
        session_id: str,
        projection_name: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM projection_checkpoints
                WHERE session_id = ? AND projection_name = ?
                """,
                (str(session_id), str(projection_name)[:120]),
            ).fetchone()
        return dict(row) if row is not None else None

    async def list_projection_checkpoints(
        self,
        session_id: str,
    ) -> list[dict[str, Any]]:
        return await self._run(self._list_projection_checkpoints, session_id)

    def _list_projection_checkpoints(
        self,
        session_id: str,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM projection_checkpoints
                WHERE session_id = ?
                ORDER BY projection_name
                """,
                (str(session_id),),
            ).fetchall()
        return [dict(row) for row in rows]

    # ── D1 Schema 20：主动投递目标 ─────────────────────────────────────

    async def upsert_delivery_target(
        self,
        *,
        platform_instance_id: str,
        message_type: str,
        target_id: str,
        session_id: str = "",
        unified_origin: str = "",
        target_kind: str = "player",
        verified_binding: bool = False,
        source: str = "",
    ) -> dict[str, Any]:
        """保存/更新投递目标；以 (平台实例, 消息类型, 目标ID) 为唯一键。"""
        return await self._run(
            self._upsert_delivery_target,
            platform_instance_id,
            message_type,
            target_id,
            session_id,
            unified_origin,
            target_kind,
            verified_binding,
            source,
        )

    def _upsert_delivery_target(
        self,
        platform_instance_id: str,
        message_type: str,
        target_id: str,
        session_id: str,
        unified_origin: str,
        target_kind: str,
        verified_binding: bool,
        source: str,
    ) -> dict[str, Any]:
        platform_instance_id = str(platform_instance_id or "").strip()
        message_type = str(message_type or "").strip().lower()
        target_id = str(target_id or "").strip()
        if not platform_instance_id or not target_id:
            raise ValueError("投递目标必须包含平台实例与目标 ID")
        if message_type not in {"group", "private", "channel", "webui_only"}:
            raise ValueError("未知的目标消息类型")
        target_kind = str(target_kind or "player").strip().lower()
        if target_kind not in {"player", "dm", "admin", "system"}:
            raise ValueError("未知的目标身份")
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO delivery_targets(
                        id, session_id, platform_instance_id, message_type,
                        target_id, unified_origin, target_kind,
                        verified_binding, source, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(platform_instance_id, message_type, target_id)
                    DO UPDATE SET
                        -- 空值表示「不修改」，避免绑定状态更新抹掉已保存来源。
                        session_id = CASE
                            WHEN excluded.session_id <> ''
                            THEN excluded.session_id
                            ELSE delivery_targets.session_id END,
                        unified_origin = CASE
                            WHEN excluded.unified_origin <> ''
                            THEN excluded.unified_origin
                            ELSE delivery_targets.unified_origin END,
                        target_kind = excluded.target_kind,
                        verified_binding = excluded.verified_binding,
                        source = excluded.source,
                        updated_at = excluded.updated_at
                    """,
                    (
                        new_id("target"),
                        str(session_id or ""),
                        platform_instance_id,
                        message_type,
                        target_id,
                        str(unified_origin or ""),
                        target_kind,
                        1 if verified_binding else 0,
                        str(source or "")[:160],
                        now,
                        now,
                    ),
                )
                row = connection.execute(
                    """
                    SELECT * FROM delivery_targets
                    WHERE platform_instance_id = ?
                      AND message_type = ? AND target_id = ?
                    """,
                    (platform_instance_id, message_type, target_id),
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return dict(row)

    async def get_delivery_target(
        self,
        *,
        platform_instance_id: str,
        message_type: str,
        target_id: str,
    ) -> dict[str, Any] | None:
        return await self._run(
            self._get_delivery_target,
            platform_instance_id,
            message_type,
            target_id,
        )

    def _get_delivery_target(
        self,
        platform_instance_id: str,
        message_type: str,
        target_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM delivery_targets
                WHERE platform_instance_id = ?
                  AND message_type = ? AND target_id = ?
                """,
                (
                    str(platform_instance_id),
                    str(message_type),
                    str(target_id),
                ),
            ).fetchone()
        return dict(row) if row is not None else None

    async def list_delivery_targets(
        self,
        session_id: str = "",
        *,
        platform_instance_id: str = "",
        message_type: str = "",
    ) -> list[dict[str, Any]]:
        return await self._run(
            self._list_delivery_targets,
            session_id,
            platform_instance_id,
            message_type,
        )

    def _list_delivery_targets(
        self,
        session_id: str,
        platform_instance_id: str,
        message_type: str,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if session_id:
            clauses.append("session_id = ?")
            values.append(str(session_id))
        if platform_instance_id:
            clauses.append("platform_instance_id = ?")
            values.append(str(platform_instance_id))
        if message_type:
            clauses.append("message_type = ?")
            values.append(str(message_type))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM delivery_targets{where}
                ORDER BY created_at, id
                """,
                tuple(values),
            ).fetchall()
        return [dict(row) for row in rows]
