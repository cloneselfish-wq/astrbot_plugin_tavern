from __future__ import annotations

from .supplement_support import *


class SupplementReceiptsRepositoryMixin:
    @staticmethod
    def _offer_revision(meta: Mapping[str, Any]) -> int:
        value = meta.get("revision", 1)
        if isinstance(value, bool):
            raise RuntimeError("角色补充 revision 损坏")
        try:
            revision = int(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuntimeError("角色补充 revision 损坏") from exc
        if revision < 1:
            raise RuntimeError("角色补充 revision 损坏")
        return revision

    def _supplement_action_replay_locked(
        self,
        connection: Any,
        *,
        idempotency_key: str,
        fingerprint: str,
    ) -> dict[str, Any] | None:
        row = connection.execute(
            """
            SELECT * FROM supplement_action_receipts
            WHERE idempotency_key=?
            """,
            (idempotency_key,),
        ).fetchone()
        return replay_receipt(row, fingerprint=fingerprint)

    def _write_supplement_receipt_locked(
        self,
        connection: Any,
        *,
        idempotency_key: str,
        session_id: str,
        participant_id: str,
        offer_id: str,
        action: str,
        expected_revision: int,
        fingerprint: str,
        event_id: str,
        result: Mapping[str, Any],
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO supplement_action_receipts(
                idempotency_key, session_id, participant_id, offer_id,
                action, expected_revision, request_fingerprint, event_id,
                result_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                idempotency_key,
                session_id,
                participant_id,
                offer_id,
                action,
                expected_revision,
                fingerprint,
                event_id,
                json_dump(dict(result)),
                created_at,
            ),
        )

    def _append_supplement_event_locked(
        self,
        connection: Any,
        *,
        session_id: str,
        participant_id: str,
        turn_no: int,
        actor: str,
        idempotency_key: str,
        action: str,
        created_at: str,
    ) -> tuple[str, int]:
        labels = {
            "confirm": "角色资料已确认。",
            "postpone": "角色资料已暂缓。",
            "reject": "角色资料候选已更换。",
            "cancel": "角色资料补充已取消。",
        }
        event_id = stable_event_id(
            idempotency_key,
            f"supplement-{action}",
        )
        append_event(
            connection,
            session_id=session_id,
            turn_no=turn_no,
            role="system",
            actor_id=actor,
            content=labels[action],
            meta={
                "kind": "card.supplement",
                "visibility": "character",
                "title": "角色资料",
                "summary": labels[action],
                "affected_modules": ["character"],
                "character_ref": participant_id,
            },
            event_id=event_id,
            created_at=created_at,
        )
        row = connection.execute(
            """
            SELECT seq FROM session_events
            WHERE session_id=? AND event_id=?
            """,
            (session_id, event_id),
        ).fetchone()
        return event_id, int(row["seq"] if row else 0)
