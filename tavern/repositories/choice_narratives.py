"""Atomic emergency NarrativeDocument edits for the choice workflow."""

from __future__ import annotations

from .workflow_support import *
from ..contracts.narrative_document import (
    NARRATIVE_DOCUMENT_SCHEMA_ID,
    NarrativeDocument,
    canonical_narrative_json,
    narrative_document_to_plain_text,
    narrative_text_sha256,
    parse_narrative_document,
)


class ChoiceNarrativesRepositoryMixin:
    async def emergency_edit_last_narrative(
        self,
        session_id: str,
        narrative_document: NarrativeDocument | Mapping[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._emergency_edit_last_narrative,
            session_id,
            (
                narrative_document.to_dict()
                if isinstance(narrative_document, NarrativeDocument)
                else dict(narrative_document)
            ),
            actor_id,
        )

    def _emergency_edit_last_narrative(
        self,
        session_id: str,
        narrative_document: dict[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        document = parse_narrative_document(
            narrative_document,
            dialogue_expected=False,
        )
        content = narrative_document_to_plain_text(document)
        document_json = canonical_narrative_json(document)
        document_text_hash = narrative_text_sha256(document)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                row = connection.execute(
                    """
                    SELECT * FROM events
                    WHERE session_id = ? AND role = 'narrator'
                    ORDER BY seq DESC LIMIT 1
                    """,
                    (session_id,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("当前副本还没有故事正文")
                stored_document = connection.execute(
                    "SELECT * FROM story_documents WHERE event_id=?",
                    (row["id"],),
                ).fetchone()
                if stored_document is None:
                    raise DatabaseNotFoundError(
                        "当前故事缺少 NarrativeDocument，不能原地修改"
                    )
                meta = json_load(row["meta_json"], {})
                meta = meta if isinstance(meta, dict) else {}
                revisions = list(meta.get("admin_revisions") or [])
                revisions.append(
                    {
                        "actor_id": actor_id,
                        "previous_text_sha256": stored_document[
                            "text_sha256"
                        ],
                        "at": utc_now(),
                    }
                )
                meta["admin_revisions"] = revisions[-10:]
                connection.execute(
                    "UPDATE events SET content = ?, meta_json = ? WHERE id = ?",
                    (content, json_dump(meta), row["id"]),
                )
                connection.execute(
                    """
                    UPDATE story_documents SET
                        schema=?, document_json=?, plain_text=?, text_sha256=?
                    WHERE event_id=?
                    """,
                    (
                        NARRATIVE_DOCUMENT_SCHEMA_ID,
                        document_json,
                        content,
                        document_text_hash,
                        row["id"],
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "rescue.edit_narrative",
                    row["id"],
                    {"turn_no": row["turn_no"]},
                )
                updated = connection.execute(
                    "SELECT * FROM events WHERE id = ?", (row["id"],)
                ).fetchone()
                connection.execute("COMMIT")
                return self._event(updated)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def emergency_append_narrative(
        self,
        session_id: str,
        narrative_document: NarrativeDocument | Mapping[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._emergency_append_narrative,
            session_id,
            (
                narrative_document.to_dict()
                if isinstance(narrative_document, NarrativeDocument)
                else dict(narrative_document)
            ),
            actor_id,
        )

    def _emergency_append_narrative(
        self,
        session_id: str,
        narrative_document: dict[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        document = parse_narrative_document(
            narrative_document,
            dialogue_expected=False,
        )
        content = narrative_document_to_plain_text(document)
        document_json = canonical_narrative_json(document)
        document_text_hash = narrative_text_sha256(document)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                now = utc_now()
                event_id = append_event(
                    connection,
                    session_id=session_id,
                    turn_no=session["turn_no"],
                    role="narrator",
                    actor_id=actor_id,
                    actor_name="管理员过渡",
                    content=content,
                    meta={"admin_bridge": True},
                    created_at=now,
                )
                connection.execute(
                    """
                    INSERT INTO story_documents(
                        event_id, session_id, turn_no, schema,
                        document_json, plain_text, text_sha256, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        session_id,
                        int(session["turn_no"] or 0),
                        NARRATIVE_DOCUMENT_SCHEMA_ID,
                        document_json,
                        content,
                        document_text_hash,
                        now,
                    ),
                )
                connection.execute(
                    """
                    UPDATE session_opening_decisions SET
                        frozen=1,
                        frozen_at=CASE WHEN frozen_at='' THEN ? ELSE frozen_at END,
                        updated_at=?
                    WHERE session_id=?
                    """,
                    (now, now, session_id),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "rescue.bridge_narrative",
                    event_id,
                    {"turn_no": session["turn_no"]},
                )
                row = connection.execute(
                    "SELECT * FROM events WHERE id = ?", (event_id,)
                ).fetchone()
                connection.execute("COMMIT")
                return self._event(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise


__all__ = ["ChoiceNarrativesRepositoryMixin"]
