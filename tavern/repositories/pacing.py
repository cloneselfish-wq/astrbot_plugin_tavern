"""C5 story pacing preview/commit repository."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from ..database_support import *
from ..story_pacing import build_pacing_plan, validate_pacing_blockers
from ..twp.commands import apply_command
from .events import append_event


def _hash(value: Mapping[str, Any]) -> str:
    material = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class PacingRepositoryMixin:
    async def preview_story_pacing(
        self,
        *,
        session_id: str,
        action: str,
        target_ref: str = "",
        expected_session_revision: int | None = None,
        actor_id: str,
        source: str,
        reason: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._preview_story_pacing,
            session_id,
            action,
            target_ref,
            expected_session_revision,
            actor_id,
            source,
            reason,
        )

    def _preview_story_pacing(
        self,
        session_id: str,
        action: str,
        target_ref: str,
        expected_session_revision: int | None,
        actor_id: str,
        source: str,
        reason: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            session = connection.execute(
                "SELECT * FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if not session:
                raise DatabaseNotFoundError("副本不存在")
            revision = int(session["revision"])
            if (
                expected_session_revision is not None
                and int(expected_session_revision) != revision
            ):
                raise DatabaseConflictError(
                    f"剧情预览修订冲突：预期 {expected_session_revision}，当前 {revision}"
                )
            config = connection.execute(
                """
                SELECT world_snapshot_json FROM instance_configs
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            world = (
                json_load(config["world_snapshot_json"], {})
                if config
                else self._world_snapshot_for(
                    connection,
                    str(session["world_id"] or ""),
                )
            )
            state = json_load(session["world_state_json"], {})
            from ..protocol.runtime import flatten_runtime, runtime_from_state

            runtime = flatten_runtime(runtime_from_state(state))
            pending_cards = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM character_cards cc
                JOIN participants p ON p.character_card_id = cc.id
                WHERE p.session_id = ?
                  AND p.card_status IN ('submitted', 'pending', 'needs_review')
                """,
                (session_id,),
            ).fetchone()
            session_view = {
                "id": session_id,
                "revision": revision,
                "card_review_blocked": bool(
                    pending_cards and int(pending_cards["total"] or 0) > 0
                ),
            }
            plan = build_pacing_plan(
                world=world,
                runtime=runtime,
                session=session_view,
                action=action,
                target_ref=target_ref,
                expected_session_revision=revision,
            )
            now = utc_now()
            connection.execute(
                """
                INSERT INTO pacing_previews(
                    plan_id, session_id, expected_session_revision,
                    preview_hash, plan_json, actor_id, source, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(plan_id) DO UPDATE SET
                    expected_session_revision = excluded.expected_session_revision,
                    preview_hash = excluded.preview_hash,
                    plan_json = excluded.plan_json,
                    actor_id = excluded.actor_id,
                    source = excluded.source,
                    reason = excluded.reason,
                    created_at = excluded.created_at
                """,
                (
                    plan["plan_id"],
                    session_id,
                    revision,
                    plan["preview_hash"],
                    json_dump(plan),
                    clean_text(actor_id, max_chars=128),
                    clean_text(source, max_chars=80),
                    clean_text(reason, max_chars=400),
                    now,
                ),
            )
            return plan

    async def commit_story_pacing(
        self,
        *,
        session_id: str,
        plan_id: str,
        preview_hash: str,
        expected_session_revision: int,
        idempotency_key: str,
        actor_id: str,
        source: str,
        reason: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._commit_story_pacing,
            session_id,
            plan_id,
            preview_hash,
            expected_session_revision,
            idempotency_key,
            actor_id,
            source,
            reason,
        )

    def _commit_story_pacing(
        self,
        session_id: str,
        plan_id: str,
        preview_hash: str,
        expected_session_revision: int,
        idempotency_key: str,
        actor_id: str,
        source: str,
        reason: str,
    ) -> dict[str, Any]:
        request = {
            "session_id": session_id,
            "plan_id": plan_id,
            "preview_hash": preview_hash,
            "expected_session_revision": int(expected_session_revision),
            "actor_id": actor_id,
            "source": source,
            "reason": reason,
        }
        input_hash = _hash(request)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    """
                    SELECT input_hash, result_json
                    FROM operation_commits
                    WHERE operation_id = ? AND session_id = ?
                    """,
                    (idempotency_key, session_id),
                ).fetchone()
                if existing:
                    if str(existing["input_hash"] or "") != input_hash:
                        raise DatabaseConflictError(
                            "幂等操作 ID 已用于另一份剧情推进请求"
                        )
                    result = json_load(existing["result_json"], {})
                    result["replayed"] = True
                    connection.execute("COMMIT")
                    return result

                preview = connection.execute(
                    """
                    SELECT * FROM pacing_previews
                    WHERE plan_id = ? AND session_id = ?
                    """,
                    (plan_id, session_id),
                ).fetchone()
                if not preview:
                    raise DatabaseConflictError(
                        "剧情预览不存在或已经失效，请重新预览。"
                    )
                if str(preview["preview_hash"] or "") != str(preview_hash or ""):
                    raise DatabaseConflictError(
                        "剧情预览内容不一致，请重新预览。"
                    )
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not session:
                    raise DatabaseNotFoundError("副本不存在")
                revision = int(session["revision"])
                if revision != int(expected_session_revision):
                    raise DatabaseConflictError(
                        f"剧情提交修订冲突：预期 {expected_session_revision}，当前 {revision}"
                    )
                stored_plan = json_load(preview["plan_json"], {})
                config = connection.execute(
                    """
                    SELECT world_snapshot_json FROM instance_configs
                    WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                world = (
                    json_load(config["world_snapshot_json"], {})
                    if config
                    else self._world_snapshot_for(
                        connection,
                        str(session["world_id"] or ""),
                    )
                )
                state = json_load(session["world_state_json"], {})
                from ..protocol.runtime import (
                    flatten_runtime,
                    hydrate_runtime,
                    runtime_from_state,
                    store_runtime,
                )

                runtime_root = runtime_from_state(state)
                runtime = flatten_runtime(runtime_root)
                pending_cards = connection.execute(
                    """
                    SELECT COUNT(*) AS total
                    FROM participants
                    WHERE session_id = ?
                      AND card_status IN ('submitted', 'pending', 'needs_review')
                    """,
                    (session_id,),
                ).fetchone()
                regenerated = build_pacing_plan(
                    world=world,
                    runtime=runtime,
                    session={
                        "id": session_id,
                        "revision": revision,
                        "card_review_blocked": bool(
                            pending_cards and int(pending_cards["total"] or 0) > 0
                        ),
                    },
                    action=str(stored_plan.get("action") or ""),
                    target_ref=str(stored_plan.get("target_scene") or ""),
                    expected_session_revision=revision,
                )
                if regenerated["preview_hash"] != str(preview_hash or ""):
                    raise DatabaseConflictError(
                        "剧情状态已变化，原预览已经失效，请重新预览。"
                    )
                blockers = validate_pacing_blockers(regenerated)
                if blockers:
                    details = "；".join(
                        str(item.get("message") or item.get("code") or "未知阻塞")
                        for item in blockers
                    )
                    raise ValueError(f"剧情推进被阻塞：{details}")

                snapshot_id = self._insert_snapshot(
                    connection,
                    session,
                    f"C5推进前-{plan_id[-12:]}",
                    "pacing_precommit",
                    actor_id,
                    replace=False,
                )
                action = str(regenerated.get("action") or "")
                if action in {"transition", "close_scene"}:
                    target_scene = str(regenerated.get("target_scene") or "")
                    command_result = apply_command(
                        world,
                        state,
                        {
                            "domain": "scene",
                            "action": "transition",
                            "targets": [target_scene],
                            "operator": actor_id or "dm",
                            "reason": reason or "剧情节奏控制器确认转场",
                            "idempotency_key": f"{idempotency_key}:scene",
                            "payload": {"force": True},
                            "visibility": "public",
                            "expected_revision": int(runtime.get("revision", 0) or 0),
                        },
                        root_operation_id=idempotency_key,
                    )
                    state = dict(command_result["state"])
                    summary = str(command_result["summary"])
                    events = list(command_result.get("events") or [])
                else:
                    events = []
                    if action == "host_beat":
                        summary = "主持人已确认一次不替玩家作决定的剧情推进。"
                    elif action == "skip_routine":
                        summary = "主持人已跳过无风险且不改变选择结果的琐碎过程。"
                    elif action == "next_clue":
                        clue_ref = str(
                            (regenerated.get("operations") or [{}])[0].get(
                                "target_ref"
                            )
                            or ""
                        )
                        opportunities = list(
                            runtime.get("story_opportunities") or []
                        )
                        if clue_ref and clue_ref not in opportunities:
                            opportunities.append(clue_ref)
                        runtime["story_opportunities"] = opportunities
                        summary = "主持人已创建下一线索的调查入口，未直接公开线索内容。"
                    else:
                        chapter_ref = str(
                            (regenerated.get("operations") or [{}])[0].get(
                                "target_ref"
                            )
                            or ""
                        )
                        runtime["current_chapter"] = chapter_ref
                        summary = "主持人已在前置条件满足后推进章节。"
                    runtime["revision"] = int(runtime.get("revision", 0) or 0) + 1
                    event = {
                        "type": f"pacing.{action}",
                        "operator": actor_id,
                        "reason": reason,
                        "root_operation_id": idempotency_key,
                    }
                    event_log = list(runtime.get("event_log") or [])
                    event_log.append(event)
                    runtime["event_log"] = event_log[-80:]
                    events.append(event)
                    store_runtime(
                        state,
                        hydrate_runtime(
                            runtime,
                            artifact_id=str(
                                runtime_root.get("artifact_id")
                                or world.get("artifact_id")
                                or ""
                            ),
                            enabled_modules=list(
                                runtime_root.get("enabled_modules") or []
                            ),
                        ),
                    )

                now = utc_now()
                connection.execute(
                    """
                    UPDATE sessions
                    SET world_state_json = ?, revision = revision + 1,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (json_dump(state), now, session_id),
                )
                event_id = append_event(
                    connection,
                    session_id=session_id,
                    turn_no=int(session["turn_no"] or 0),
                    role="director",
                    actor_id=actor_id,
                    actor_name="剧情节奏控制器",
                    content=summary,
                    meta={
                        "kind": "story_pacing",
                        "plan_id": plan_id,
                        "action": action,
                        "source": source,
                        "events": events,
                    },
                    created_at=now,
                )
                result = {
                    "ok": True,
                    "session_id": session_id,
                    "plan_id": plan_id,
                    "preview_hash": preview_hash,
                    "action": action,
                    "summary": summary,
                    "snapshot_id": snapshot_id,
                    "event_id": event_id,
                    "revision": revision + 1,
                    "events": events,
                    "operation_id": idempotency_key,
                    "replayed": False,
                }
                connection.execute(
                    """
                    INSERT INTO operation_commits(
                        operation_id, session_id, input_hash, status,
                        result_json, rollback_json, created_at, updated_at
                    ) VALUES (?, ?, ?, 'completed', ?, ?, ?, ?)
                    """,
                    (
                        idempotency_key,
                        session_id,
                        input_hash,
                        json_dump(result),
                        json_dump({"snapshot_id": snapshot_id}),
                        now,
                        now,
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    f"story_pacing.{action}",
                    plan_id,
                    {
                        "source": source,
                        "reason": reason,
                        "snapshot_id": snapshot_id,
                        "revision_before": revision,
                        "revision_after": revision + 1,
                    },
                )
                connection.execute(
                    "DELETE FROM pacing_previews WHERE plan_id = ?",
                    (plan_id,),
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise


__all__ = ["PacingRepositoryMixin"]
