"""persistent domain services.

The methods in this mixin are host-independent.  They expose transaction
boundaries for actors, AI companion configuration, miniprogram bindings and
room joins, module runtime status, and public choice-recovery receipts.
"""

from __future__ import annotations

import re

from ..database_support import *
from ..lifecycle import (
    attribute_maps,
    find_profession_preset,
    semantic_field_key,
)


def _public_ref(kind: str, value: object) -> str:
    digest = hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:12].upper()
    return f"public:{kind}:{digest}"


def _binding_view(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": "tavern-principal-binding/1.0.0-rc10",
        "binding_ref": _public_ref("binding", row.get("id")),
        "principal_kind": str(row.get("principal_kind") or ""),
        "provider": str(row.get("provider") or ""),
        "display_name": str(row.get("display_name") or ""),
        "status": str(row.get("status") or ""),
        "revision": int(row.get("revision") or 0),
    }


def _actor_view(row: Mapping[str, Any]) -> dict[str, Any]:
    actor_status = str(
        row.get("actor_status")
        if row.get("actor_status") is not None
        else row.get("status") or ""
    )
    action_status = str(row.get("action_status") or actor_status)
    revision = int(
        row.get("instance_revision")
        or row.get("revision")
        or row.get("actor_revision")
        or 0
    )
    updated_at = max(
        (
            str(value or "")
            for value in (
                row.get("instance_updated_at"),
                row.get("actor_updated_at"),
                row.get("updated_at"),
            )
            if value
        ),
        default="",
    )
    return {
        "schema": "tavern-actor/1.0.0-rc10",
        "actor_ref": _public_ref(
            "actor", row.get("actor_id_internal") or row.get("id")
        ),
        "kind": str(row.get("actor_kind") or ""),
        "display_name": str(row.get("display_name") or ""),
        "status": actor_status,
        "action_status": action_status,
        "mode": str(row.get("mode") or ""),
        "revision": revision,
        "updated_at": updated_at,
    }


_AI_VISUAL_PRIVATE_KEYS = frozenset(
    {
        "actor_id",
        "current_operation_id",
        "decision_policy",
        "decision_policy_json",
        "lease_owner",
        "operation_id",
        "preset_id",
        "provider_id",
        "provider_policy",
        "provider_policy_json",
        "session_id",
        "system_prompt",
        "trace_id",
    }
)


def _ai_visual_input(value: Any) -> Any:
    """Remove technical AI material before semantic projection receives it."""

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key).strip().lower()
            if (
                name in _AI_VISUAL_PRIVATE_KEYS
                or "provider" in name
                or "prompt" in name
            ):
                continue
            result[str(key)] = _ai_visual_input(item)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_ai_visual_input(item) for item in value]
    return value


def _ai_visual_state_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """Return the authoritative in-process state needed by the console party lens.

    The repository deliberately excludes preset/provider/decision/lease data.
    ``state`` and ``profile`` remain internal inputs: the console semantic projector
    applies role visibility and emits only labels, values and safe statuses.
    """

    actor = _actor_view(row)
    return {
        **actor,
        "state": _ai_visual_input(
            json_load(row.get("actor_state_json", "{}"), {})
        ),
        "profile": _ai_visual_input(
            json_load(row.get("frozen_profile_json", "{}"), {})
        ),
        "awaiting_confirmation": bool(row.get("awaiting_confirmation")),
        "inventory_supported": True,
    }


def _decision_view(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": "tavern-ai-decision/1.0.0-rc10",
        "actor_ref": _public_ref("actor", row.get("actor_id")),
        "operation_ref": _public_ref("operation", row.get("operation_id")),
        "choice_set_ref": (
            _public_ref("choice", row.get("choice_set_id"))
            if row.get("choice_set_id")
            else ""
        ),
        "status": str(row.get("status") or ""),
        "decision": _ai_visual_input(
            json_load(row.get("public_projection_json", "{}"), {})
        ),
        "updated_at": str(row.get("updated_at") or ""),
    }


def _materialize_ai_profile(
    world: Mapping[str, Any],
    preset: Mapping[str, Any],
) -> dict[str, Any]:
    frozen = dict(preset)
    if isinstance(frozen.get("stats"), Mapping):
        return frozen
    try:
        template = card_template(world)
        profession_field = semantic_field_key(
            template,
            "actor.identity.profession",
        )
        primary_field = semantic_field_key(
            template,
            "actor.stats.primary",
        )
        secondary_field = semantic_field_key(
            template,
            "actor.stats.secondary",
        )
        profession_ref = str(preset.get("profession_ref") or "")
        profession = find_profession_preset(template, profession_ref)
        base = profession.get("base_attributes") or profession.get(
            "attributes"
        )
        base = base if isinstance(base, Mapping) else {}
        _label_to_key, key_to_label = attribute_maps(template)
        ranked = sorted(
            (
                (int(value), str(key))
                for key, value in base.items()
                if str(key) in key_to_label
            ),
            key=lambda item: (-item[0], item[1]),
        )
        if (
            not profession_field
            or not primary_field
            or not secondary_field
            or len(ranked) < 2
        ):
            return frozen
        fields = {
            profession_field: profession_ref,
            primary_field: key_to_label[ranked[0][1]],
            secondary_field: key_to_label[ranked[1][1]],
        }
        frozen["stats"] = resolve_profession_stats(
            template,
            fields,
            require_complete=True,
        )
        frozen["stat_selection"] = {
            "primary": ranked[0][1],
            "secondary": ranked[1][1],
            "source": "deterministic_profession_profile",
        }
    except (KeyError, TypeError, ValueError):
        # Worlds without an attribute contract remain valid AI worlds, but
        # check choices will be rejected instead of inventing modifiers.
        pass
    return frozen


class ChoiceRecoveryRepositoryMixin:
    async def record_choice_recovery(
        self,
        *,
        session_id: str,
        choice_set_id: str,
        operation_id: str,
        failure_kind: str,
        repair_count: int,
        fallback_version: str,
        public_message: str,
        trace_id: str,
        status: str,
        idempotency_key: str,
        provider_class: str = "",
        resolution_summary: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self._run(
            self._record_choice_recovery,
            session_id,
            choice_set_id,
            operation_id,
            failure_kind,
            repair_count,
            fallback_version,
            public_message,
            trace_id,
            status,
            idempotency_key,
            provider_class,
            dict(resolution_summary or {}),
        )

    def _record_choice_recovery(
        self,
        session_id: str,
        choice_set_id: str,
        operation_id: str,
        failure_kind: str,
        repair_count: int,
        fallback_version: str,
        public_message: str,
        trace_id: str,
        status: str,
        idempotency_key: str,
        provider_class: str,
        resolution_summary: Mapping[str, Any],
    ) -> dict[str, Any]:
        if status not in {"repaired", "fallback", "failed", "cancelled"}:
            raise ValueError("未知的选项恢复状态")
        now = utc_now()
        with self._connect() as connection:
            row = self._insert_choice_recovery_locked(
                connection,
                session_id=session_id,
                choice_set_id=choice_set_id,
                operation_id=operation_id,
                recovery={
                    "failure_kind": failure_kind,
                    "repair_count": repair_count,
                    "fallback_version": fallback_version,
                    "message": public_message,
                    "trace_id": trace_id,
                    "status": status,
                    "idempotency_key": idempotency_key,
                    "provider_class": provider_class,
                    "resolution_summary": dict(resolution_summary or {}),
                },
                now=now,
            )
        return self._choice_recovery_view(row)

    def _insert_choice_recovery_locked(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        choice_set_id: str,
        operation_id: str,
        recovery: Mapping[str, Any],
        now: str,
    ) -> sqlite3.Row:
        status = str(recovery.get("status") or "")
        if status not in {"repaired", "fallback", "failed", "cancelled"}:
            raise ValueError("未知的选项恢复状态")
        idempotency_key = str(
            recovery.get("idempotency_key")
            or f"{operation_id}:choice-recovery"
        )
        connection.execute(
            """
            INSERT INTO choice_recovery_receipts(
                id, session_id, choice_set_id, operation_id,
                provider_class, failure_kind, repair_count, fallback_version,
                resolution_summary_json, public_message, trace_id,
                status, idempotency_key, created_at
            ) VALUES (?, ?, NULLIF(?,''), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(idempotency_key) DO NOTHING
            """,
            (
                new_id("choice-recovery"),
                session_id,
                choice_set_id,
                operation_id,
                clean_text(recovery.get("provider_class"), max_chars=80),
                clean_text(recovery.get("failure_kind"), max_chars=100),
                max(0, min(1, int(recovery.get("repair_count") or 0))),
                clean_text(recovery.get("fallback_version"), max_chars=80),
                json_dump(dict(recovery.get("resolution_summary") or {})),
                clean_text(recovery.get("message"), max_chars=1000),
                clean_text(recovery.get("trace_id"), max_chars=80),
                status,
                idempotency_key,
                now,
            ),
        )
        row = connection.execute(
            "SELECT * FROM choice_recovery_receipts WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if row is None:
            raise RuntimeError("选项恢复收据写入失败")
        return row

    @staticmethod
    def _choice_recovery_view(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "schema": "tavern-choice-recovery-receipt/1.0.0-rc10",
            "status": str(row["status"]),
            "failure_kind": str(row["failure_kind"]),
            "repair_count": int(row["repair_count"]),
            "fallback_version": str(row["fallback_version"]),
            "message": str(row["public_message"]),
            "trace_id": str(row["trace_id"]),
            "resolution_summary": json_load(
                row["resolution_summary_json"],
                {},
            ),
        }

    async def latest_choice_recovery(
        self,
        session_id: str,
        choice_set_id: str = "",
    ) -> dict[str, Any] | None:
        return await self._run(
            self._latest_choice_recovery,
            str(session_id),
            str(choice_set_id),
        )

    def _latest_choice_recovery(
        self,
        session_id: str,
        choice_set_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            if choice_set_id:
                row = connection.execute(
                    """
                    SELECT * FROM choice_recovery_receipts
                    WHERE session_id=? AND choice_set_id=?
                    ORDER BY created_at DESC, id DESC LIMIT 1
                    """,
                    (session_id, choice_set_id),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT * FROM choice_recovery_receipts
                    WHERE session_id=?
                    ORDER BY created_at DESC, id DESC LIMIT 1
                    """,
                    (session_id,),
                ).fetchone()
        return self._choice_recovery_view(row) if row is not None else None


__all__ = ["ChoiceRecoveryRepositoryMixin"]
