from __future__ import annotations

from .story_support import *
from ..recovery_ranges import parse_recovery_json
from ..contracts.narrative_document import (
    NarrativeDocument,
    legacy_text_fallback,
    narrative_document_to_plain_text,
    narrative_text_sha256,
    parse_narrative_document,
    project_public_narrative_document,
)


_STORY_PROBLEM_CONTRACTS: dict[str, dict[str, Any]] = {
    "legacy_invalid": {
        "code": "story.legacy_record_invalid",
        "operation": "读取旧故事记录",
        "reason": "这条明确标记的旧故事记录未通过安全检查。",
        "automatic": (
            "系统未改写旧记录，也未把文本猜测为新结构；"
            "上次成功读取的故事仍保留。"
        ),
        "recovery": "请从已验证的旧版备份恢复该记录。",
        "next_step": (
            "请由主持人发送：/团 存档列表；"
            "确认后发送：/团 读档 <名称>"
        ),
        "retryable": False,
    },
    "missing": {
        "code": "story.document_missing",
        "operation": "读取当前故事",
        "reason": "当前故事缺少可验证的结构化正文。",
        "automatic": (
            "系统未改写故事，也未猜测缺失结构；"
            "上次成功读取的故事仍保留。"
        ),
        "recovery": "请由主持人刷新后检查最近一次故事提交。",
        "next_step": (
            "请先刷新当前故事；若仍缺失，"
            "请由主持人发送：/团 存档列表"
        ),
        "retryable": True,
    },
    "corrupt": {
        "code": "story.document_corrupt",
        "operation": "校验当前故事",
        "reason": "当前故事的结构化正文与可验证内容不一致。",
        "automatic": (
            "系统未改写或重新生成故事，也未猜测结构；"
            "上次成功读取的故事仍保留。"
        ),
        "recovery": "请从已验证备份恢复，不要重新生成这段故事。",
        "next_step": (
            "请由主持人发送：/团 存档列表；"
            "确认后发送：/团 读档 <名称>"
        ),
        "retryable": False,
    },
}


def _story_problem(kind: str) -> dict[str, Any]:
    contract = _STORY_PROBLEM_CONTRACTS[kind]
    return {**contract, "message": contract["reason"]}


class StoryLogRepositoryMixin:
    def _event_with_document(
        self,
        connection: Any,
        row: Any,
    ) -> dict[str, Any]:
        event = self._event(row)
        if str(event.get("role") or "") != "narrator":
            return event
        stored = connection.execute(
            "SELECT * FROM story_documents WHERE event_id=?",
            (event["id"],),
        ).fetchone()
        if stored is None:
            meta = (
                dict(event.get("meta"))
                if isinstance(event.get("meta"), Mapping)
                else {}
            )
            if meta.get("legacy_record") is True:
                try:
                    legacy = legacy_text_fallback(
                        event.get("content"), legacy_record=True
                    )
                    event.update(legacy.to_dict())
                    event["narrative_document"] = None
                    event["narrative_problem"] = ""
                    return event
                except Exception:
                    event["narrative_document"] = None
                    event["narrative_problem"] = _story_problem(
                        "legacy_invalid"
                    )
                    return event
            event["narrative_document"] = None
            event["narrative_problem"] = _story_problem("missing")
            return event
        try:
            document = parse_narrative_document(
                json_load(stored["document_json"], {}),
                dialogue_expected=False,
            )
            if (
                narrative_document_to_plain_text(document)
                != str(stored["plain_text"] or "")
                or narrative_text_sha256(document)
                != str(stored["text_sha256"] or "")
                or str(event.get("content") or "")
                != str(stored["plain_text"] or "")
            ):
                raise ValueError("stored text mismatch")
            event["narrative_document"] = (
                project_public_narrative_document(document)
            )
            event["narrative_problem"] = ""
        except Exception:
            event["narrative_document"] = None
            event["narrative_problem"] = _story_problem("corrupt")
        return event

    async def latest_public_story_event(
        self,
        session_id: str,
    ) -> dict[str, Any] | None:
        return await self._run(self._latest_public_story_event, session_id)

    def _latest_public_story_event(
        self,
        session_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = latest_public_story_row(connection, session_id)
            return (
                self._event_with_document(connection, row)
                if row is not None
                else None
            )

    async def recent_events(
        self,
        session_id: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        return await self._run(self._recent_events, session_id, limit)

    def _recent_events(
        self,
        session_id: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(200, int(limit)))
        with self._connect() as connection:
            session = connection.execute(
                """
                SELECT s.history_floor_seq, sr.recovery_json
                FROM sessions s
                LEFT JOIN session_rule_states sr ON sr.session_id = s.id
                WHERE s.id = ?
                """,
                (session_id,),
            ).fetchone()
            if not session:
                raise DatabaseNotFoundError("会话不存在")
            recovery = parse_recovery_json(
                session["recovery_json"]
                if session["recovery_json"] is not None
                else "{}"
            )
            excluded_ranges = recovery.excluded_event_ranges
            exclusions = "".join(
                " AND NOT (seq BETWEEN ? AND ?)"
                for _ in excluded_ranges
            )
            parameters: list[Any] = [
                session_id,
                session["history_floor_seq"],
            ]
            for start, end in excluded_ranges:
                parameters.extend((start, end))
            parameters.append(limit)
            rows = connection.execute(
                f"""
                SELECT * FROM events
                WHERE session_id = ? AND seq >= ?
                {exclusions}
                ORDER BY seq DESC LIMIT ?
                """,
                tuple(parameters),
            ).fetchall()
            return [
                self._event_with_document(connection, row)
                for row in reversed(rows)
            ]

    async def get_story_document(
        self,
        event_id: str,
    ) -> NarrativeDocument:
        return await self._run(self._get_story_document, str(event_id))

    def _get_story_document(self, event_id: str) -> NarrativeDocument:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM story_documents WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if row is None:
                raise DatabaseNotFoundError("故事结构不存在")
            document = parse_narrative_document(
                json_load(row["document_json"], {}),
                dialogue_expected=False,
            )
            if (
                narrative_document_to_plain_text(document)
                != str(row["plain_text"] or "")
                or narrative_text_sha256(document)
                != str(row["text_sha256"] or "")
            ):
                raise ValueError("故事结构与确定性正文不一致")
            return document

    async def append_ooc(
        self,
        session_id: str,
        actor_id: str,
        actor_name: str,
        content: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._append_ooc,
            session_id,
            actor_id,
            actor_name,
            content,
        )

    def _append_ooc(
        self,
        session_id: str,
        actor_id: str,
        actor_name: str,
        content: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            session = connection.execute(
                "SELECT * FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if not session:
                raise DatabaseNotFoundError("会话不存在")
            event_id = append_event(
                connection,
                session_id=session_id,
                turn_no=session["turn_no"],
                role="ooc",
                actor_id=actor_id,
                actor_name=actor_name,
                content=content,
                created_at=utc_now(),
            )
            row = connection.execute(
                "SELECT * FROM events WHERE id = ?",
                (event_id,),
            ).fetchone()
            return self._event(row)

    async def list_memories(
        self,
        session_id: str,
        query: str = "",
        limit: int = 100,
        *,
        include_invalidated: bool = False,
    ) -> list[dict[str, Any]]:
        return await self._run(
            self._list_memories,
            session_id,
            query,
            limit,
            include_invalidated,
        )

    async def list_visible_memories_page(
        self,
        session_id: str,
        *,
        viewer_role: str,
        viewer_id: str = "",
        viewer_participant_ref: str = "",
        query: str = "",
        offset: int = 0,
        page_size: int = 20,
        include_invalidated: bool = False,
    ) -> dict[str, Any]:
        """Return a permission-cropped, SQL-paged fact ledger.

        ``total`` counts only rows visible to this viewer.  Private rows and
        even their count remain outside the projection.  Player-private facts
        are visible only when ``scope_id`` resolves to that player's current
        participant identity in the same session.
        """

        return await self._run(
            self._list_visible_memories_page,
            str(session_id),
            str(viewer_role),
            str(viewer_id),
            str(viewer_participant_ref),
            str(query),
            int(offset),
            int(page_size),
            bool(include_invalidated),
        )

    def _list_visible_memories_page(
        self,
        session_id: str,
        viewer_role: str,
        viewer_id: str,
        viewer_participant_ref: str,
        query: str,
        offset: int,
        page_size: int,
        include_invalidated: bool,
    ) -> dict[str, Any]:
        session_id = clean_text(session_id, max_chars=300)
        if not session_id:
            raise ValueError("缺少要查看的副本")
        role = clean_text(viewer_role, max_chars=40).lower()
        if role == "dm":
            role = "host"
        if role == "moderator":
            role = "host"
        if role not in {"admin", "host", "player"}:
            raise PermissionError("当前身份不能查看长期记忆")
        viewer_id = clean_text(viewer_id, max_chars=300)
        viewer_participant_ref = clean_text(
            viewer_participant_ref,
            max_chars=300,
        )
        if role == "player" and not viewer_id and not viewer_participant_ref:
            raise PermissionError("缺少可验证的玩家身份")
        normalized_query = clean_text(query, max_chars=200).casefold()
        normalized_offset = max(0, int(offset or 0))
        normalized_page_size = max(1, min(100, int(page_size or 20)))

        clauses = ["m.session_id = ?"]
        parameters: list[Any] = [session_id]
        visibility = "COALESCE(mg.visibility, 'public')"
        if role == "host":
            clauses.append(f"{visibility} IN ('public', 'host')")
        elif role == "player":
            clauses.append(
                f"""
                (
                    {visibility} = 'public'
                    OR (
                        {visibility} = 'private'
                        AND EXISTS (
                            SELECT 1 FROM participants viewer_pt
                            WHERE viewer_pt.session_id = m.session_id
                              AND (
                                  viewer_pt.id = ?
                                  OR viewer_pt.group_user_id = ?
                                  OR viewer_pt.private_user_id = ?
                              )
                              AND viewer_pt.participation_status NOT IN (
                                  'retired', 'archived'
                              )
                              AND m.scope_id IN (
                                  viewer_pt.id,
                                  viewer_pt.group_user_id,
                                  viewer_pt.private_user_id
                              )
                        )
                    )
                )
                """
            )
            parameters.extend(
                [viewer_participant_ref, viewer_id, viewer_id]
            )
        if not include_invalidated:
            clauses.append("COALESCE(mg.invalidated, 0) = 0")
        if normalized_query:
            clauses.append(
                """
                (
                    instr(lower(m.content), ?) > 0
                    OR instr(lower(m.tags_json), ?) > 0
                    OR instr(lower(m.kind), ?) > 0
                )
                """
            )
            parameters.extend([normalized_query] * 3)
        where = " AND ".join(clauses)
        base = f"""
            FROM memories m
            LEFT JOIN memory_governance mg ON mg.memory_id = m.id
            WHERE {where}
        """
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if exists is None:
                raise DatabaseNotFoundError("会话不存在")
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) {base}",
                    tuple(parameters),
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"""
                SELECT m.*,
                       mg.visibility AS governance_visibility,
                       mg.locked AS governance_locked,
                       mg.pinned AS governance_pinned,
                       mg.invalidated AS governance_invalidated,
                       mg.supersedes_id AS governance_supersedes_id,
                       mg.conflict_status AS governance_conflict_status,
                       mg.note AS governance_note
                {base}
                ORDER BY COALESCE(mg.pinned, 0) DESC,
                         COALESCE(mg.locked, 0) DESC,
                         m.importance DESC,
                         m.salience DESC,
                         m.updated_at DESC,
                         m.id DESC
                LIMIT ? OFFSET ?
                """,
                (
                    *parameters,
                    normalized_page_size,
                    normalized_offset,
                ),
            ).fetchall()
        items = [self._memory(row) for row in rows]
        return {
            "items": items,
            "total": total,
            "offset": normalized_offset,
            "page_size": normalized_page_size,
            "has_more": normalized_offset + len(items) < total,
        }

    @staticmethod
    def _query_terms(query: str) -> set[str]:
        compact = "".join(str(query).lower().split())
        if not compact:
            return set()
        terms = {compact}
        terms.update(
            compact[index : index + 2]
            for index in range(max(0, len(compact) - 1))
        )
        return {term for term in terms if term}

    def _list_memories(
        self,
        session_id: str,
        query: str,
        limit: int,
        include_invalidated: bool,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(500, int(limit)))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT m.*,
                       mg.visibility AS governance_visibility,
                       mg.locked AS governance_locked,
                       mg.pinned AS governance_pinned,
                       mg.invalidated AS governance_invalidated,
                       mg.supersedes_id AS governance_supersedes_id,
                       mg.conflict_status AS governance_conflict_status,
                       mg.note AS governance_note
                FROM memories m
                LEFT JOIN memory_governance mg ON mg.memory_id = m.id
                WHERE m.session_id = ?
                  AND (? OR COALESCE(mg.invalidated, 0) = 0)
                ORDER BY COALESCE(mg.pinned, 0) DESC,
                         COALESCE(mg.locked, 0) DESC,
                         m.importance DESC, m.salience DESC, m.updated_at DESC
                LIMIT 500
                """,
                (session_id, int(include_invalidated)),
            ).fetchall()
            memories = [self._memory(row) for row in rows]
            terms = self._query_terms(query)
            if terms:
                for memory in memories:
                    haystack = "".join(
                        (
                            memory["content"]
                            + " "
                            + " ".join(memory["tags"])
                            + " "
                            + memory["kind"]
                        )
                        .lower()
                        .split()
                    )
                    matches = sum(term in haystack for term in terms)
                    memory["_score"] = (
                        matches * 5
                        + memory["importance"] * 2
                        + float(memory["salience"])
                    )
                memories = [
                    memory
                    for memory in memories
                    if (
                        memory["locked"]
                        or memory["pinned"]
                        or memory.get("_score", 0) > memory["importance"] * 2
                    )
                ]
                memories.sort(
                    key=lambda item: (
                        int(item["pinned"]),
                        int(item["locked"]),
                        item.get("_score", 0),
                    ),
                    reverse=True,
                )
            protected = [
                memory
                for memory in memories
                if memory["locked"] or memory["pinned"]
            ]
            selected = protected + [
                memory
                for memory in memories
                if memory not in protected
            ][: max(0, limit - len(protected))]
            archived = connection.execute(
                """
                SELECT readonly FROM session_archives
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            if selected and not archived:
                now = utc_now()
                connection.executemany(
                    """
                    UPDATE memories
                    SET last_accessed_at = ?, salience = MIN(10, salience + 0.05)
                    WHERE id = ?
                    """,
                    [(now, item["id"]) for item in selected],
                )
            for item in selected:
                item.pop("_score", None)
            return selected

    async def commit_turn(
        self,
        *,
        session_id: str,
        expected_revision: int,
        player_id: str,
        player_user_id: str,
        player_name: str,
        player_input: str,
        narrative: str,
        narrative_document: NarrativeDocument | Mapping[str, Any],
        world_state: Mapping[str, Any],
        memories: Sequence[Mapping[str, Any]],
        check_payload: Mapping[str, Any] | None,
        model_payload: Mapping[str, Any] | None,
        director_note: str,
        auto_snapshot_interval: int,
        store_model_payload: bool,
        workflow: Mapping[str, Any] | None = None,
        actor_kind: str = "human",
        actor_id: str = "",
        operation_id: str = "",
        operation_result: Mapping[str, Any] | None = None,
        item_ops: Sequence[Mapping[str, Any]] | None = None,
        economy_ops: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return await self._run(
            self._commit_turn_sync,
            session_id,
            expected_revision,
            player_id,
            player_user_id,
            player_name,
            player_input,
            narrative,
            (
                narrative_document.to_dict()
                if isinstance(narrative_document, NarrativeDocument)
                else dict(narrative_document)
            ),
            dict(world_state),
            [dict(item) for item in memories],
            dict(check_payload or {}),
            dict(model_payload or {}),
            clean_text(director_note, max_chars=500),
            auto_snapshot_interval,
            store_model_payload,
            dict(workflow or {}),
            str(actor_kind or "human"),
            str(actor_id or ""),
            operation_id,
            dict(operation_result or {}),
            [dict(op) for op in (item_ops or [])],
            [dict(op) for op in (economy_ops or [])],
        )
