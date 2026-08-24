"""Transaction-local structured fate consequence commit orchestration."""

from __future__ import annotations

from .fate_state import _FATE_PREVIEW_PREFIX, _preview_expiry
from .fate_support import *


class FateConsequenceCommitRepositoryMixin:
    """Commit consequences or stage actor-owned lethal previews atomically."""

    def _commit_actor_fate_locked(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        world: Mapping[str, Any],
        consequences: Sequence[Mapping[str, Any]],
        event_ref: str,
        actor_id: str,
        turn_no: int,
        trigger_revision: int,
        now: str,
        expire_existing: bool = True,
    ) -> dict[str, Any]:
        normalized = [
            dict(item)
            for item in consequences
            if isinstance(item, Mapping)
        ]
        frozen_config = connection.execute(
            "SELECT world_snapshot_json FROM instance_configs WHERE session_id=?",
            (session_id,),
        ).fetchone()
        if frozen_config is None and not normalized:
            open_window = connection.execute(
                """
                SELECT 1 FROM rescue_windows
                WHERE session_id=? AND status='open' LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            pending_preview = connection.execute(
                """
                SELECT 1 FROM operation_commits
                WHERE session_id=? AND operation_id LIKE 'actor-fate-preview:%'
                  AND status='pending_consent'
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            terminal_conditions = parse_terminal_conditions(world)
            if open_window or pending_preview or terminal_conditions:
                raise DatabaseConflictError(
                    "副本缺少冻结世界快照，无法安全结算救援、预览过期或终局。"
                    "系统没有修改角色命运；请先修复副本世界配置。"
                )
            # No consequence and no fate lifecycle capable of mutating state:
            # legacy/non-fate sessions must not be blocked merely because an
            # unrelated story commit lacks an instance snapshot.
            return {
                "applied": [],
                "expired": [],
                "expired_previews": [],
                "terminal": None,
            }
        # Never trust the caller's installed-world object for a session
        # lifecycle decision.  The per-instance snapshot is the authority.
        world, frozen_world_revision = self._frozen_fate_world_locked(
            connection,
            session_id,
        )
        contract = parse_actor_fate(world)
        if normalized and not bool(contract.get("declared")):
            raise ValueError("当前世界未启用角色命运，不能提交结构化后果")
        if len(normalized) > 16:
            raise ValueError(
                "单次最多提交 16 条角色命运后果；系统未写入任何后果，"
                "请拆分后重试。"
            )
        expired = (
            self._expire_rescue_windows_locked(
                connection,
                session_id=session_id,
                world=world,
                current_turn=turn_no,
                event_ref=event_ref,
                now=now,
            )
            if expire_existing
            else []
        )
        expired_previews = self._expire_fate_previews_locked(
            connection,
            session_id=session_id,
            current_turn=turn_no,
            now=now,
        )
        applied: list[dict[str, Any]] = []
        for consequence in normalized:
            participant = self._resolve_fate_participant_locked(
                connection,
                session_id=session_id,
                actor_ref=str(consequence.get("target_actor") or ""),
            )
            fate = self._initialize_player_fate_locked(
                connection,
                participant=participant,
                world=world,
                now=now,
            )
            if fate is None:
                raise ValueError("目标角色缺少可结算的命运状态")
            protection_ids = {
                str(item.get("id") or "")
                for item in _sequence(contract.get("protection_resources"))
                if isinstance(item, Mapping) and str(item.get("id") or "")
            }
            resource_rows = connection.execute(
                """
                SELECT * FROM character_resources
                WHERE session_id = ? AND character_id = ?
                """,
                (session_id, str(participant["id"])),
            ).fetchall()
            protection = {
                str(row["resource_ref"]): int(row["current"] or 0)
                for row in resource_rows
                if str(row["resource_ref"]) in protection_ids
            }
            open_window_row = connection.execute(
                """
                SELECT * FROM rescue_windows
                WHERE session_id = ? AND character_id = ?
                  AND status = 'open'
                ORDER BY opened_at, id LIMIT 1
                """,
                (session_id, str(participant["id"])),
            ).fetchone()
            open_window = dict(open_window_row) if open_window_row else {}
            if open_window:
                open_window.update(
                    {
                        "allowed_rescue_commands": json_load(
                            open_window.get("allowed_rescue_commands_json"),
                            [],
                        ),
                        "success_transition": json_load(
                            open_window.get("success_transition_json"),
                            {},
                        ),
                        "failure_transition": json_load(
                            open_window.get("failure_transition_json"),
                            {},
                        ),
                        "command_labels": json_load(
                            open_window.get("command_labels_json"),
                            {},
                        ),
                    }
                )
            if (
                str(consequence.get("severity") or "").strip().lower()
                == "lethal"
                and not open_window
            ):
                plan = resolve_structured_consequence(
                    contract=contract,
                    actor_ref=str(participant["id"]),
                    current_state=str(fate["state"]),
                    consequence=consequence,
                    sequence=int(fate["revision"] or 0) + 1,
                    created_at=now,
                    event_ref=event_ref,
                    protection=protection,
                    allow_direct_terminal=False,
                )
                record = _mapping(plan.get("record"))
                transition = find_transition(
                    contract,
                    str(fate["state"]),
                    str(record.get("to_state") or ""),
                )
                target = state_definition(
                    contract,
                    str(record.get("to_state") or ""),
                )
                if (
                    transition is None
                    or target is None
                    or bool(target.get("terminal"))
                    or not bool(transition.get("opens_rescue_window"))
                ):
                    raise ValueError(
                        "致命命运预览缺少合法的非终态救援转换。"
                        "系统没有修改角色状态；请修复世界命运规则。"
                    )
                window_kind = str(
                    transition.get("rescue_window_kind") or "default"
                )
                window = _window_definition(contract, window_kind)
                if not window:
                    raise ValueError(
                        "致命命运预览引用了未声明的救援窗口。"
                        "系统没有修改角色状态；请修复世界命运规则。"
                    )
                command_labels = _mapping(window.get("command_labels"))
                alternatives = [
                    str(command_labels.get(command) or command)
                    for command in _sequence(
                        window.get("allowed_rescue_commands")
                    )
                    if str(command).strip()
                ]
                alternatives.append("拒绝本次致命命运，保持当前状态")
                preview_input = {
                    "session_id": session_id,
                    "participant_id": str(participant["id"]),
                    "from_state": str(fate["state"]),
                    "target_state": str(record.get("to_state") or ""),
                    "expected_fate_revision": int(fate["revision"] or 0),
                    "source": str(consequence.get("source") or ""),
                    "reason": str(consequence.get("reason") or ""),
                    "alternatives": alternatives,
                    "rescue_window_kind": window_kind,
                    "expires_on": _preview_expiry(
                        window,
                        turn_no=turn_no,
                    ),
                    "world_revision": frozen_world_revision,
                    "event_ref": event_ref,
                    "transition": {
                        "from": str(fate["state"]),
                        "to": str(record.get("to_state") or ""),
                    },
                }
                preview_hash = request_fingerprint(preview_input)
                pending_rows = connection.execute(
                    """
                    SELECT * FROM operation_commits
                    WHERE session_id = ? AND operation_id LIKE ?
                      AND status = 'pending_consent'
                    ORDER BY created_at, operation_id
                    """,
                    (session_id, _FATE_PREVIEW_PREFIX + "%"),
                ).fetchall()
                reused_preview: dict[str, Any] = {}
                for pending_row in pending_rows:
                    pending_data = json_load(
                        pending_row["result_json"],
                        {},
                    )
                    candidate = _mapping(pending_data.get("preview"))
                    if (
                        str(candidate.get("participant_id") or "")
                        == str(participant["id"])
                        and int(
                            candidate.get("expected_fate_revision") or 0
                        )
                        == int(fate["revision"] or 0)
                    ):
                        if str(candidate.get("request_sha256") or "") != (
                            preview_hash
                        ):
                            raise DatabaseConflictError(
                                "该角色已有不同内容的致命命运预览待处理。"
                                "系统没有覆盖原预览或角色状态；"
                                "请由角色本人先确认或拒绝原预览。"
                            )
                        reused_preview = {
                            **candidate,
                            "operation_id": str(
                                pending_row["operation_id"]
                            ),
                        }
                        break
                if reused_preview:
                    applied.append(
                        {
                            "character_id": str(participant["id"]),
                            "character_name": str(
                                participant.get("character_name") or ""
                            ),
                            "state": str(fate["state"]),
                            "state_label": str(fate["state_label"] or ""),
                            "can_act": bool(fate["can_act"]),
                            "terminal": bool(fate["terminal"]),
                            "pending_preview": True,
                            "preview_reused": True,
                            "state_changed": False,
                        }
                    )
                    continue
                preview_id = _FATE_PREVIEW_PREFIX + hashlib.sha256(
                    (
                        f"{session_id}\0{participant['id']}\0"
                        f"{event_ref}\0{preview_hash}"
                    ).encode("utf-8")
                ).hexdigest()[:24]
                preview = {
                    **preview_input,
                    "actor_name": str(
                        participant.get("character_name")
                        or participant.get("display_name")
                        or "角色"
                    ),
                    "request_sha256": preview_hash,
                    "status": "pending_consent",
                    "created_at": now,
                }
                connection.execute(
                    """
                    INSERT INTO operation_commits(
                        operation_id, session_id, input_hash, status,
                        result_json, rollback_json, created_at, updated_at
                    ) VALUES (?, ?, ?, 'pending_consent', ?, '{}', ?, ?)
                    ON CONFLICT(operation_id) DO NOTHING
                    """,
                    (
                        preview_id,
                        session_id,
                        preview_hash,
                        json_dump({"preview": preview}),
                        now,
                        now,
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "actor_fate.preview.created",
                    preview_id,
                    {
                        "participant_id": str(participant["id"]),
                        "expected_fate_revision": int(
                            fate["revision"] or 0
                        ),
                        "rescue_window_kind": window_kind,
                    },
                )
                insert_session_event(
                    connection,
                    session_id=session_id,
                    event_id=f"{preview_id}:event",
                    type_="event:actor_fate.preview_created",
                    actor_ref=str(participant["group_user_id"] or ""),
                    command_id=preview_id,
                    payload={
                        "title": "角色命运预览待本人确认",
                        "summary": (
                            "系统已保留原命运状态，等待目标角色本人确认或拒绝。"
                        ),
                        "affected_modules": ["actor_fate"],
                    },
                    visibility="private",
                    created_at=now,
                )
                applied.append(
                    {
                        "character_id": str(participant["id"]),
                        "character_name": str(
                            participant.get("character_name") or ""
                        ),
                        "state": str(fate["state"]),
                        "state_label": str(fate["state_label"] or ""),
                        "can_act": bool(fate["can_act"]),
                        "terminal": bool(fate["terminal"]),
                        "pending_preview": True,
                        "preview_reused": False,
                        "state_changed": False,
                    }
                )
                continue
            result = resolve_structured_consequence(
                contract=contract,
                actor_ref=str(participant["id"]),
                current_state=str(fate["state"]),
                consequence=consequence,
                sequence=int(fate["revision"] or 0) + 1,
                created_at=now,
                event_ref=event_ref,
                protection=protection,
                open_window=open_window,
                allow_direct_terminal=bool(
                    _mapping(contract.get("policy")).get(
                        "direct_terminal_authorized"
                    )
                ),
            )
            if bool(result.get("skipped")):
                applied.append(
                    {
                        "character_id": str(participant["id"]),
                        "character_name": str(
                            participant.get("character_name") or ""
                        ),
                        "state": str(fate["state"]),
                        "state_label": str(fate["state_label"] or ""),
                        "can_act": bool(fate["can_act"]),
                        "terminal": bool(fate["terminal"]),
                        "rescue_window": bool(open_window),
                        "rescue_window_kind": str(
                            open_window.get("kind") or ""
                        ),
                        "rescue_window_until": str(
                            open_window.get("expires_on") or ""
                        ),
                        "effective_severity": str(
                            result.get("effective_severity") or ""
                        ),
                        "protection_consumed": "",
                        "skipped": True,
                        "message": str(result.get("message") or ""),
                    }
                )
                continue
            record = dict(result["record"])
            consumed_resource = str(
                record.get("consumed_protection_resource") or ""
            )
            if consumed_resource:
                updated = connection.execute(
                    """
                    UPDATE character_resources
                    SET current = current - 1, updated_at = ?
                    WHERE session_id = ? AND character_id = ?
                      AND resource_ref = ? AND current > 0
                    """,
                    (
                        now,
                        session_id,
                        str(participant["id"]),
                        consumed_resource,
                    ),
                )
                if updated.rowcount != 1:
                    raise DatabaseConflictError("保护资源已被其他操作消耗")
            window_kind = str(result.get("rescue_window_kind") or "")
            state = self._apply_fate_transition_locked(
                connection,
                session_id=session_id,
                character_id=str(participant["id"]),
                contract=contract,
                record=record,
                now=now,
                turn_no=turn_no,
                protection_consumed=bool(consumed_resource),
                window_definition=_window_definition(contract, window_kind),
            )
            applied.append(
                {
                    **state,
                    "character_name": str(
                        participant.get("character_name") or ""
                    ),
                    "effective_severity": str(
                        result.get("effective_severity") or ""
                    ),
                    "protection_consumed": consumed_resource,
                }
            )
        terminal = self._evaluate_terminal_locked(
            connection,
            session_id=session_id,
            actor_id=actor_id,
            trigger_revision=trigger_revision,
            world=world,
        )
        return {
            "applied": applied,
            "expired": expired,
            "expired_previews": expired_previews,
            "terminal": terminal,
        }


__all__ = ["FateConsequenceCommitRepositoryMixin"]
