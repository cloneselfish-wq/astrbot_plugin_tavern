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


class AiDecisionRepositoryMixin:
    @staticmethod
    def _decision_by_public_operation_ref(
        connection: sqlite3.Connection,
        operation_ref: str,
    ) -> sqlite3.Row:
        row = next(
            (
                item
                for item in connection.execute(
                    """
                    SELECT * FROM ai_companion_decision_receipts
                    ORDER BY created_at DESC, id DESC
                    """
                ).fetchall()
                if _public_ref("operation", item["operation_id"])
                == str(operation_ref)
            ),
            None,
        )
        if row is None:
            raise DatabaseNotFoundError("AI 决策不存在或已经失效")
        return row

    async def pending_ai_decision(
        self,
        session_id: str,
    ) -> dict[str, Any] | None:
        return await self._run(self._pending_ai_decision, session_id)

    def _pending_ai_decision(
        self,
        session_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM ai_companion_decision_receipts
                WHERE session_id=? AND status='awaiting_confirmation'
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        return _decision_view(dict(row)) if row is not None else None

    async def claim_ai_confirmation(
        self,
        *,
        session_id: str,
        operation_ref: str,
        expected_session_revision: int,
        lease_owner: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._claim_ai_confirmation,
            session_id,
            operation_ref,
            expected_session_revision,
            lease_owner,
        )

    def _claim_ai_confirmation(
        self,
        session_id: str,
        operation_ref: str,
        expected_session_revision: int,
        lease_owner: str,
    ) -> dict[str, Any]:
        if not operation_ref or not lease_owner:
            raise ValueError("AI 确认缺少操作引用或租约")
        now = utc_now()
        lease_expires = (
            datetime.now(timezone.utc) + timedelta(seconds=90)
        ).isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._decision_by_public_operation_ref(
                    connection,
                    operation_ref,
                )
                if str(row["session_id"]) != str(session_id):
                    raise DatabaseConflictError("AI 决策不属于当前副本")
                if str(row["status"]) != "awaiting_confirmation":
                    connection.execute("COMMIT")
                    return {
                        **_decision_view(dict(row)),
                        "replayed": True,
                    }
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id=?",
                    (session_id,),
                ).fetchone()
                instance = connection.execute(
                    "SELECT * FROM ai_companion_instances WHERE actor_id=?",
                    (row["actor_id"],),
                ).fetchone()
                choice = connection.execute(
                    """
                    SELECT * FROM choice_sets
                    WHERE id=? AND session_id=? AND status='active'
                    """,
                    (row["choice_set_id"], session_id),
                ).fetchone()
                if session is None or instance is None or choice is None:
                    raise DatabaseConflictError(
                        "副本、AI 队友或当前选项已经更新"
                    )
                if int(session["revision"]) != int(
                    expected_session_revision
                ) or int(session["revision"]) != int(
                    row["session_revision"]
                ):
                    raise DatabaseConflictError(
                        "副本已更新，请刷新后重新确认"
                    )
                if choice["actor_id"] != row["actor_id"]:
                    raise DatabaseConflictError(
                        "当前选项已经转交给其他行动角色"
                    )
                if str(instance["mode"]) == "paused":
                    raise InvalidTransitionError("AI 队友当前已暂停")
                decision = json_load(row["decision_json"], {})
                choice_key = str(decision.get("choice_key") or "").upper()
                if choice_key not in CHOICE_KEYS:
                    raise DatabaseConflictError("待确认选择内容已损坏")
                connection.execute(
                    """
                    UPDATE ai_companion_decision_receipts
                    SET status='planned', updated_at=? WHERE id=?
                    """,
                    (now, row["id"]),
                )
                connection.execute(
                    """
                    UPDATE ai_companion_instances SET
                        status='acting', current_operation_id=?,
                        lease_owner=?, leased_at=?, lease_expires_at=?,
                        updated_at=?
                    WHERE actor_id=?
                    """,
                    (
                        row["operation_id"],
                        lease_owner,
                        now,
                        lease_expires,
                        now,
                        row["actor_id"],
                    ),
                )
                connection.execute("COMMIT")
                return {
                    **_decision_view(dict(row)),
                    "replayed": False,
                    "operation_id": str(row["operation_id"]),
                    "choice_set_id": str(row["choice_set_id"]),
                    "choice_key": choice_key,
                    "actor_ref": _public_ref("actor", row["actor_id"]),
                    "session_revision": int(row["session_revision"]),
                }
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    async def discard_ai_decision(
        self,
        *,
        session_id: str,
        operation_ref: str,
        pause_actor: bool = False,
    ) -> dict[str, Any]:
        return await self._run(
            self._discard_ai_decision,
            session_id,
            operation_ref,
            pause_actor,
        )

    def _discard_ai_decision(
        self,
        session_id: str,
        operation_ref: str,
        pause_actor: bool,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._decision_by_public_operation_ref(
                    connection,
                    operation_ref,
                )
                if str(row["session_id"]) != str(session_id):
                    raise DatabaseConflictError("AI 决策不属于当前副本")
                if str(row["status"]) == "awaiting_confirmation":
                    connection.execute(
                        """
                        UPDATE ai_companion_decision_receipts
                        SET status='discarded', updated_at=? WHERE id=?
                        """,
                        (now, row["id"]),
                    )
                    connection.execute(
                        """
                        UPDATE ai_companion_instances SET
                            mode=CASE WHEN ? THEN 'paused' ELSE mode END,
                            status=CASE WHEN ? THEN 'paused' ELSE 'active' END,
                            current_operation_id='', lease_owner='',
                            leased_at='', lease_expires_at='',
                            revision=revision+1, updated_at=?
                        WHERE actor_id=?
                        """,
                        (
                            int(bool(pause_actor)),
                            int(bool(pause_actor)),
                            now,
                            row["actor_id"],
                        ),
                    )
                updated = connection.execute(
                    """
                    SELECT * FROM ai_companion_decision_receipts WHERE id=?
                    """,
                    (row["id"],),
                ).fetchone()
                connection.execute("COMMIT")
                return _decision_view(dict(updated))
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def ai_companion_decision_context(
        self,
        *,
        session_id: str,
        actor_ref: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._ai_companion_decision_context,
            session_id,
            actor_ref,
        )

    def _ai_companion_decision_context(
        self,
        session_id: str,
        actor_ref: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            actor = self._actor_by_public_ref(connection, actor_ref)
            if str(actor["session_id"]) != str(session_id):
                raise DatabaseConflictError("AI 队友不属于当前副本")
            instance = connection.execute(
                """
                SELECT * FROM ai_companion_instances WHERE actor_id=?
                """,
                (actor["id"],),
            ).fetchone()
            if instance is None:
                raise DatabaseNotFoundError("AI 队友实例不存在")
            session = connection.execute(
                "SELECT revision FROM sessions WHERE id=?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise DatabaseNotFoundError("副本不存在")
        return {
            "schema": "tavern-ai-decision-context/1.0.0-rc10",
            "actor": _actor_view(
                {
                    **dict(actor),
                    "mode": instance["mode"],
                    "preset_id": instance["preset_id"],
                    "revision": instance["revision"],
                }
            ),
            "profile": json_load(instance["frozen_profile_json"], {}),
            "decision_policy": json_load(
                instance["decision_policy_json"],
                {},
            ),
            "mode": str(instance["mode"]),
            "revision": int(instance["revision"]),
            "session_revision": int(session["revision"]),
        }

    async def claim_ai_decision(
        self,
        *,
        session_id: str,
        actor_ref: str,
        choice_set_id: str,
        operation_id: str,
        expected_actor_revision: int,
        expected_session_revision: int,
        lease_owner: str,
        lease_seconds: int,
        idempotency_key: str,
        trace_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._claim_ai_decision,
            session_id,
            actor_ref,
            choice_set_id,
            operation_id,
            expected_actor_revision,
            expected_session_revision,
            lease_owner,
            lease_seconds,
            idempotency_key,
            trace_id,
        )

    def _claim_ai_decision(
        self,
        session_id: str,
        actor_ref: str,
        choice_set_id: str,
        operation_id: str,
        expected_actor_revision: int,
        expected_session_revision: int,
        lease_owner: str,
        lease_seconds: int,
        idempotency_key: str,
        trace_id: str,
    ) -> dict[str, Any]:
        if not all(
            str(value or "").strip()
            for value in (
                actor_ref,
                choice_set_id,
                operation_id,
                lease_owner,
                idempotency_key,
                trace_id,
            )
        ):
            raise ValueError("AI 决策缺少必要的操作、租约或追踪信息")
        now = utc_now()
        lease_expires = (
            datetime.now(timezone.utc)
            + timedelta(seconds=max(15, min(300, int(lease_seconds))))
        ).isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                replay = connection.execute(
                    """
                    SELECT * FROM ai_companion_decision_receipts
                    WHERE idempotency_key=?
                    """,
                    (idempotency_key,),
                ).fetchone()
                if replay and str(replay["status"]) != "planned":
                    connection.execute("COMMIT")
                    return {**_decision_view(dict(replay)), "replayed": True}
                actor = self._actor_by_public_ref(connection, actor_ref)
                if str(actor["session_id"]) != str(session_id):
                    raise DatabaseConflictError("AI 队友不属于当前副本")
                instance = connection.execute(
                    """
                    SELECT * FROM ai_companion_instances WHERE actor_id=?
                    """,
                    (actor["id"],),
                ).fetchone()
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id=?",
                    (session_id,),
                ).fetchone()
                if instance is None or session is None:
                    raise DatabaseNotFoundError("AI 队友或副本不存在")
                if str(instance["mode"]) == "paused":
                    raise InvalidTransitionError("AI 队友当前已暂停")
                if int(instance["revision"]) != int(
                    expected_actor_revision
                ):
                    raise DatabaseConflictError("AI 队友状态已更新")
                if int(session["revision"]) != int(
                    expected_session_revision
                ):
                    raise DatabaseConflictError("副本状态已更新")
                if replay and (
                    str(replay["session_id"]) != str(session_id)
                    or str(replay["actor_id"]) != str(actor["id"])
                    or str(replay["choice_set_id"] or "")
                    != str(choice_set_id)
                    or str(replay["operation_id"]) != str(operation_id)
                ):
                    raise DatabaseConflictError(
                        "相同幂等键已用于其他 AI 决策"
                    )
                if (
                    str(instance["lease_expires_at"] or "") > now
                    and str(instance["lease_owner"] or "") != lease_owner
                ):
                    raise DatabaseConflictError("AI 决策正在由其他 worker 处理")
                if replay:
                    previous_owner = str(instance["lease_owner"] or "")
                    lease_was_expired = (
                        str(instance["lease_expires_at"] or "") <= now
                    )
                    connection.execute(
                        """
                        UPDATE ai_companion_instances SET
                            status='acting', current_operation_id=?,
                            lease_owner=?, leased_at=?, lease_expires_at=?,
                            updated_at=?
                        WHERE actor_id=?
                        """,
                        (
                            operation_id,
                            lease_owner,
                            now,
                            lease_expires,
                            now,
                            actor["id"],
                        ),
                    )
                    connection.execute("COMMIT")
                    return {
                        **_decision_view(dict(replay)),
                        "replayed": False,
                        "recovered_lease": bool(
                            lease_was_expired
                            or previous_owner != lease_owner
                        ),
                    }
                previous_operation = str(
                    instance["current_operation_id"] or ""
                )
                if (
                    previous_operation
                    and previous_operation != operation_id
                ):
                    connection.execute(
                        """
                        UPDATE ai_companion_decision_receipts SET
                            status='discarded',
                            public_projection_json=?,
                            updated_at=?
                        WHERE operation_id=? AND actor_id=?
                          AND status='planned'
                        """,
                        (
                            json_dump(
                                {
                                    "message": (
                                        "上一次 AI 决策租约已过期，"
                                        "系统已由新的 worker 安全接管。"
                                    )
                                }
                            ),
                            now,
                            previous_operation,
                            actor["id"],
                        ),
                    )
                receipt_id = new_id("ai-decision")
                connection.execute(
                    """
                    INSERT INTO ai_companion_decision_receipts(
                        id, session_id, actor_id, operation_id,
                        choice_set_id, actor_revision, session_revision,
                        decision_json, public_projection_json, status,
                        idempotency_key, trace_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, '{}', '{}', 'planned',
                              ?, ?, ?, ?)
                    """,
                    (
                        receipt_id,
                        session_id,
                        actor["id"],
                        operation_id,
                        choice_set_id,
                        expected_actor_revision,
                        expected_session_revision,
                        idempotency_key,
                        trace_id,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    UPDATE ai_companion_instances SET
                        status='acting', current_operation_id=?,
                        lease_owner=?, leased_at=?, lease_expires_at=?,
                        updated_at=?
                    WHERE actor_id=?
                    """,
                    (
                        operation_id,
                        lease_owner,
                        now,
                        lease_expires,
                        now,
                        actor["id"],
                    ),
                )
                row = connection.execute(
                    """
                    SELECT * FROM ai_companion_decision_receipts WHERE id=?
                    """,
                    (receipt_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return {**_decision_view(dict(row)), "replayed": False}
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def finish_ai_decision(
        self,
        *,
        operation_id: str,
        lease_owner: str,
        decision: Mapping[str, Any],
        public_projection: Mapping[str, Any],
        status: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._finish_ai_decision,
            operation_id,
            lease_owner,
            dict(decision),
            dict(public_projection),
            status,
        )

    def _finish_ai_decision(
        self,
        operation_id: str,
        lease_owner: str,
        decision: Mapping[str, Any],
        public_projection: Mapping[str, Any],
        status: str,
    ) -> dict[str, Any]:
        if status not in {
            "awaiting_confirmation",
            "submitted",
            "discarded",
            "failed",
        }:
            raise ValueError("未知 AI 决策完成状态")
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT * FROM ai_companion_decision_receipts
                    WHERE operation_id=?
                    """,
                    (operation_id,),
                ).fetchone()
                if row is None:
                    raise DatabaseNotFoundError("AI 决策收据不存在")
                if str(row["status"]) != "planned":
                    connection.execute("COMMIT")
                    return _decision_view(dict(row))
                instance = connection.execute(
                    """
                    SELECT * FROM ai_companion_instances WHERE actor_id=?
                    """,
                    (row["actor_id"],),
                ).fetchone()
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id=?",
                    (row["session_id"],),
                ).fetchone()
                if (
                    instance is None
                    or session is None
                    or str(instance["lease_owner"] or "") != lease_owner
                    or str(instance["current_operation_id"] or "")
                    != operation_id
                ):
                    raise DatabaseConflictError("AI 决策租约已失效")
                final_status = status
                if str(instance["mode"]) == "paused" or (
                    status != "submitted"
                    and int(session["revision"]) != int(row["session_revision"])
                ):
                    final_status = "discarded"
                connection.execute(
                    """
                    UPDATE ai_companion_decision_receipts SET
                        decision_json=?, public_projection_json=?,
                        status=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        json_dump(decision),
                        json_dump(public_projection),
                        final_status,
                        now,
                        row["id"],
                    ),
                )
                next_state = (
                    "paused"
                    if str(instance["mode"]) == "paused"
                    else "active"
                )
                connection.execute(
                    """
                    UPDATE ai_companion_instances SET
                        status=?, current_operation_id='',
                        lease_owner='', leased_at='', lease_expires_at='',
                        last_decision_receipt_json=?,
                        revision=revision+1, updated_at=?
                    WHERE actor_id=?
                    """,
                    (
                        next_state,
                        json_dump(public_projection),
                        now,
                        row["actor_id"],
                    ),
                )
                updated = connection.execute(
                    """
                    SELECT * FROM ai_companion_decision_receipts WHERE id=?
                    """,
                    (row["id"],),
                ).fetchone()
                connection.execute("COMMIT")
                return _decision_view(dict(updated))
            except Exception:
                connection.execute("ROLLBACK")
                raise

__all__ = ["AiDecisionRepositoryMixin"]
