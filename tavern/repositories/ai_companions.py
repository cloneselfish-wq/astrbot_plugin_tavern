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


class AiCompanionRepositoryMixin:
    async def configure_ai_companions(
        self,
        *,
        session_id: str,
        count: int,
        mode: str,
        expected_session_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._configure_ai_companions,
            session_id,
            count,
            mode,
            expected_session_revision,
            idempotency_key,
        )

    def _configure_ai_companions(
        self,
        session_id: str,
        count: int,
        mode: str,
        expected_session_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        count = int(count)
        if count < 0 or count > 8:
            raise ValueError("AI 队友数量必须在 0—8 之间")
        mode = str(mode or "confirm").strip().lower()
        if mode not in {"automatic", "confirm", "paused"}:
            raise ValueError("AI 队友模式必须为 automatic、confirm 或 paused")
        if not str(idempotency_key or "").strip():
            raise ValueError("配置 AI 队友需要 idempotency_key")
        request_payload = {
            "count": count,
            "mode": mode,
            "expected_session_revision": int(expected_session_revision),
        }
        input_hash = hashlib.sha256(
            json_dump(request_payload).encode("utf-8")
        ).hexdigest()
        operation_id = (
            "ai-config:"
            + hashlib.sha256(
                f"{session_id}\0{idempotency_key}".encode("utf-8")
            ).hexdigest()[:24]
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                receipt = connection.execute(
                    "SELECT * FROM operation_receipts WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if receipt is not None:
                    if str(receipt["input_hash"] or "") != input_hash:
                        raise DatabaseConflictError(
                            "相同幂等键已用于不同的 AI 队友配置"
                        )
                    if str(receipt["status"] or "") == "completed":
                        replay = json_load(receipt["result_json"], {})
                        replay["replayed"] = True
                        connection.execute("COMMIT")
                        return replay
                    raise DatabaseConflictError(
                        "AI 队友配置仍在处理中，请稍后重试"
                    )
                now = utc_now()
                connection.execute(
                    """
                    INSERT INTO operation_receipts(
                        operation_id, session_id, operation_type,
                        request_json, result_json, status, phase,
                        input_hash, created_at, updated_at
                    ) VALUES (?, ?, 'ai_companion.configure', ?, '{}',
                              'pending', 'configure', ?, ?, ?)
                    """,
                    (
                        operation_id,
                        session_id,
                        json_dump(request_payload),
                        input_hash,
                        now,
                        now,
                    ),
                )
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id=?", (session_id,)
                ).fetchone()
                if session is None:
                    raise DatabaseNotFoundError("副本不存在")
                if int(session["revision"]) != int(expected_session_revision):
                    raise DatabaseConflictError("副本状态已更新，请刷新后重试")
                config = connection.execute(
                    "SELECT world_snapshot_json FROM instance_configs WHERE session_id=?",
                    (session_id,),
                ).fetchone()
                world = json_load(config["world_snapshot_json"] if config else "", {})
                module = world.get("ai_companions") or (
                    world.get("rules", {}).get("ai_companions")
                    if isinstance(world.get("rules"), Mapping)
                    else {}
                )
                module = module if isinstance(module, Mapping) else {}
                presets = [
                    dict(item)
                    for item in module.get("presets", [])
                    if isinstance(item, Mapping)
                ]
                if count and len(presets) < 10:
                    raise ValueError("当前世界没有提供至少 10 个合法 AI 队友预设")
                existing = connection.execute(
                    """
                    SELECT a.*, i.mode, i.preset_id, i.revision AS instance_revision
                    FROM actors a JOIN ai_companion_instances i ON i.actor_id=a.id
                    WHERE a.session_id=? AND a.actor_kind='ai_companion'
                    ORDER BY a.created_at, a.id
                    """,
                    (session_id,),
                ).fetchall()
                for index in range(count):
                    preset = presets[index % len(presets)]
                    if index < len(existing):
                        actor_id = str(existing[index]["id"])
                        connection.execute(
                            """
                            UPDATE actors SET status='active',
                                revision=revision+1, updated_at=? WHERE id=?
                            """,
                            (now, actor_id),
                        )
                        connection.execute(
                            """
                            UPDATE ai_companion_instances SET mode=?, status='active',
                                revision=revision+1, updated_at=? WHERE actor_id=?
                            """,
                            (mode, now, actor_id),
                        )
                    else:
                        actor_id = new_id("actor")
                        frozen_profile = _materialize_ai_profile(
                            world,
                            preset,
                        )
                        label = clean_text(
                            preset.get("name") or f"AI 队友 {index + 1}",
                            max_chars=100,
                        )
                        connection.execute(
                            """
                            INSERT INTO actors(
                                id, session_id, actor_kind, display_name,
                                controller_kind, controller_ref, status,
                                state_json, revision, created_at, updated_at
                            ) VALUES (
                                ?, ?, 'ai_companion', ?, 'policy', '',
                                'active', '{}', 1, ?, ?
                            )
                            """,
                            (actor_id, session_id, label, now, now),
                        )
                        connection.execute(
                            """
                            INSERT INTO ai_companion_instances(
                                actor_id, session_id, preset_id, preset_version,
                                frozen_profile_json, decision_policy_json,
                                provider_policy_json, mode, status,
                                revision, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', 1, ?, ?)
                            """,
                            (
                                actor_id,
                                session_id,
                                str(preset.get("id") or ""),
                                str(preset.get("version") or "1.0.0-rc10"),
                                json_dump(frozen_profile),
                                json_dump(preset.get("decision_policy") or {}),
                                json_dump(module.get("provider_policy") or {}),
                                mode,
                                now,
                                now,
                            ),
                        )
                for row in existing[count:]:
                    connection.execute(
                        """
                        UPDATE actors SET status='retired',
                            revision=revision+1, updated_at=? WHERE id=?
                        """,
                        (now, row["id"]),
                    )
                    connection.execute(
                        """
                        UPDATE ai_companion_instances SET status='retired',
                            revision=revision+1, updated_at=? WHERE actor_id=?
                        """,
                        (now, row["id"]),
                    )
                connection.execute(
                    """
                    UPDATE sessions SET revision=revision+1, updated_at=?
                    WHERE id=? AND revision=?
                    """,
                    (now, session_id, expected_session_revision),
                )
                rows = connection.execute(
                    """
                    SELECT a.*, i.mode,
                           i.status AS action_status,
                           i.revision AS instance_revision,
                           i.updated_at AS instance_updated_at
                    FROM actors a JOIN ai_companion_instances i ON i.actor_id=a.id
                    WHERE a.session_id=? AND a.actor_kind='ai_companion'
                      AND a.status<>'retired'
                    ORDER BY a.created_at, a.id
                    """,
                    (session_id,),
                ).fetchall()
                result = {
                    "schema": "tavern-ai-companion-list/1.0.0-rc10",
                    "mode": mode,
                    "count": len(rows),
                    "items": [_actor_view(dict(row)) for row in rows],
                    "session_revision": int(expected_session_revision) + 1,
                    "replayed": False,
                }
                connection.execute(
                    """
                    UPDATE operation_receipts SET
                        result_json=?, status='completed',
                        phase='committed', committed_revision=?,
                        updated_at=?
                    WHERE operation_id=?
                    """,
                    (
                        json_dump(result),
                        int(expected_session_revision) + 1,
                        now,
                        operation_id,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return result

    async def list_ai_companions(self, session_id: str) -> dict[str, Any]:
        return await self._run(self._list_ai_companions, session_id)

    def _list_ai_companions(self, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT a.*, i.mode,
                       i.status AS action_status,
                       i.revision AS instance_revision,
                       i.updated_at AS instance_updated_at
                FROM actors a JOIN ai_companion_instances i ON i.actor_id=a.id
                WHERE a.session_id=? AND a.actor_kind='ai_companion'
                  AND a.status<>'retired'
                ORDER BY a.created_at, a.id
                """,
                (session_id,),
            ).fetchall()
            pending = connection.execute(
                """
                SELECT * FROM ai_companion_decision_receipts
                WHERE session_id=? AND status='awaiting_confirmation'
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        return {
            "schema": "tavern-ai-companion-list/1.0.0-rc10",
            "count": len(rows),
            "items": [_actor_view(dict(row)) for row in rows],
            "mode": (
                str(rows[0]["mode"])
                if rows and all(str(row["mode"]) == str(rows[0]["mode"]) for row in rows)
                else "confirm"
            ),
            "pending_decision": (
                _decision_view(dict(pending))
                if pending is not None
                else None
            ),
        }

    async def list_ai_companion_visual_states(
        self,
        session_id: str,
    ) -> dict[str, Any]:
        """Read AI actor/profile/action state for server-side console projection.

        This is not a Web DTO.  It intentionally omits database identifiers,
        preset/provider policies, prompts, leases, operations and traces.
        """

        return await self._run(
            self._list_ai_companion_visual_states,
            session_id,
        )

    def _list_ai_companion_visual_states(
        self,
        session_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    a.id AS actor_id_internal,
                    a.actor_kind AS actor_kind,
                    a.display_name AS display_name,
                    a.status AS actor_status,
                    a.state_json AS actor_state_json,
                    a.revision AS actor_revision,
                    a.updated_at AS actor_updated_at,
                    i.mode AS mode,
                    i.status AS action_status,
                    i.frozen_profile_json AS frozen_profile_json,
                    i.revision AS instance_revision,
                    i.updated_at AS instance_updated_at
                FROM actors a
                JOIN ai_companion_instances i ON i.actor_id=a.id
                WHERE a.session_id=? AND a.actor_kind='ai_companion'
                  AND a.status<>'retired' AND i.status<>'retired'
                ORDER BY a.created_at, a.id
                """,
                (session_id,),
            ).fetchall()
            pending = connection.execute(
                """
                SELECT actor_id FROM ai_companion_decision_receipts
                WHERE session_id=? AND status='awaiting_confirmation'
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        pending_actor_id = str(pending["actor_id"]) if pending is not None else ""
        items: list[dict[str, Any]] = []
        for raw in rows:
            row = dict(raw)
            row["awaiting_confirmation"] = bool(
                pending_actor_id
                and str(row.get("actor_id_internal") or "") == pending_actor_id
            )
            items.append(_ai_visual_state_view(row))
        return {
            "schema": "tavern-ai-companion-visual-state/1.0.0",
            "count": len(items),
            "items": items,
        }

__all__ = ["AiCompanionRepositoryMixin"]
