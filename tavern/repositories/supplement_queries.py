from __future__ import annotations

from .supplement_support import *


class SupplementQueriesRepositoryMixin:
    async def list_supplement_offers(
        self,
        session_id: str,
        *,
        participant_id: str = "",
        viewer_role: str = "player",
        turn_no: int | None = None,
    ) -> list[dict[str, Any]]:
        """列出活跃补充提议；普通玩家仅能看到自己的提议。"""

        return await self._run(
            self._list_supplement_offers,
            str(session_id or "").strip(),
            str(participant_id or "").strip(),
            str(viewer_role or "player"),
            turn_no,
        )

    async def supplement_context_for_private(
        self,
        private_origin: str,
    ) -> dict[str, Any] | None:
        """按真实私聊来源解析参与者与本人待确认补充项。

        普通入口只返回玩家安全视图；内部 field_key、稳定参与者 ID 与
        投递目标不会进入 BOT 文案。
        """

        context = await self._run(
            self._supplement_context_for_private,
            str(private_origin or "").strip(),
        )
        if context is None:
            return None
        offers = await self.list_supplement_offers(
            str(context["session_id"]),
            participant_id=str(context["participant_id"]),
            viewer_role="player",
        )
        return {**context, "offers": offers}

    def _supplement_context_for_private(
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

    def _list_supplement_offers(
        self,
        session_id: str,
        participant_id: str,
        viewer_role: str,
        turn_no: int | None,
    ) -> list[dict[str, Any]]:
        if not session_id:
            return []
        role = str(viewer_role or "player").strip()
        privileged = role in {"dm", "admin"}
        with self._connect() as connection:
            session = connection.execute(
                "SELECT * FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if not session:
                return []
            if turn_no is None:
                turn_no = int(session["turn_no"] or 0)
            config_row = connection.execute(
                "SELECT world_snapshot_json FROM instance_configs WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            world = json_load(config_row["world_snapshot_json"], {}) if config_row else {}
            config = supplement_config(world)
            try:
                template = card_template(world)
            except (KeyError, TypeError, ValueError):
                template = {}
            rows = connection.execute(
                """
                SELECT * FROM delivery_outbox
                WHERE session_id = ? AND kind = ?
                ORDER BY created_at ASC, id ASC
                """,
                (session_id, SUPPLEMENT_KIND),
            ).fetchall()
        participant_names: dict[str, str] = {}
        if privileged:
            with self._connect() as connection:
                for row in connection.execute(
                    "SELECT id, character_name, group_user_id FROM participants WHERE session_id = ?",
                    (session_id,),
                ).fetchall():
                    participant_names[str(row["id"])] = str(
                        row["character_name"] or ""
                    )
        result: list[dict[str, Any]] = []
        for row in rows:
            meta = json_load(row["meta_json"], {})
            if str(meta.get("kind") or "") != SUPPLEMENT_KIND:
                continue
            state = str(meta.get("state") or "offered")
            if state not in OFFER_OPEN_STATES:
                continue
            owner = str(meta.get("participant_id") or "")
            if not privileged:
                if not participant_id or owner != participant_id:
                    continue
            expired = offer_expired(meta, turn_no, config)
            stage = str(meta.get("stage") or "")
            view: dict[str, Any] = {
                "offer_id": str(row["id"]),
                "revision": int(meta.get("revision") or 1),
                "field_key": (
                    str(meta.get("field_key") or "") if privileged else None
                ),
                "field_label": str(meta.get("field_label") or ""),
                "stage": stage,
                "stage_label": (
                    stage_label(template, stage) if template else ""
                ),
                "state": "expired" if expired else state,
                "expired": expired,
                "candidates": [
                    option_view(option)
                    for option in meta.get("candidates") or []
                    if isinstance(option, Mapping)
                ],
                "free_text": bool(meta.get("free_text")),
                "fallback": bool(meta.get("fallback")),
                "offer_round": int(meta.get("offer_round") or 0),
                "expires_after_rounds": int(
                    meta.get("expires_after_rounds")
                    or config["expires_after_rounds"]
                ),
            }
            if privileged:
                view.update(
                    {
                        "participant_id": owner,
                        "character_name": str(
                            meta.get("character_name") or ""
                        )
                        or participant_names.get(owner, ""),
                        "delivery_status": str(row["status"] or ""),
                        "attempts": int(row["attempts"] or 0),
                        "last_error": str(row["last_error"] or ""),
                        "rejected_ids": list(meta.get("rejected_ids") or []),
                        "trigger_source": str(
                            meta.get("trigger_source") or ""
                        ),
                        "offer_no": int(meta.get("offer_no") or 0),
                    }
                )
            result.append(view)
        return result

    # ------------------------------------------------------------------
    # 确认
    # ------------------------------------------------------------------

    def _load_offer_context_locked(
        self,
        connection: Any,
        session_id: str,
        row: Any,
        meta: Mapping[str, Any],
    ) -> tuple[Any, Any, dict[str, Any], dict[str, Any], dict[str, int]]:
        participant = connection.execute(
            "SELECT * FROM participants WHERE id = ?",
            (str(meta.get("participant_id") or ""),),
        ).fetchone()
        if participant is None:
            raise ValueError("该补充提议归属的角色不存在")
        if str(participant["session_id"] or "") != str(session_id):
            raise ValueError("该补充提议不属于当前副本")
        session = connection.execute(
            "SELECT * FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        config_row = connection.execute(
            "SELECT world_snapshot_json FROM instance_configs WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        world = json_load(config_row["world_snapshot_json"], {}) if config_row else {}
        template = card_template(world)
        config = supplement_config(world)
        return participant, session, world, template, config

    def _offer_record(
        self,
        *,
        session_id: str,
        target: DeliveryTarget,
        text: str,
        meta: Mapping[str, Any],
        dedupe_key: str,
    ) -> dict[str, Any]:
        now = utc_now()
        return {
            "delivery_id": new_id("delivery"),
            "session_id": str(session_id),
            "audience": "private_owner",
            "target_snapshot": target.to_snapshot(),
            "message_type": SUPPLEMENT_KIND,
            "projection_snapshot": {"kind": SUPPLEMENT_KIND},
            "rendered_parts": [text],
            "next_part_index": 0,
            "status": (
                "webui_only"
                if target.message_type == TARGET_KIND_WEBUI_ONLY
                else "pending"
            ),
            "priority": 80,
            "attempts": 0,
            "next_retry_at": now,
            "last_error_code": "",
            "last_error_message": "",
            "dedupe_key": str(dedupe_key or ""),
            "created_at": now,
            "updated_at": now,
            "delivered_at": "",
            "cancelled_at": "",
            "lease_token": "",
            "lease_until": "",
            "meta": dict(meta),
            "max_attempts": 8,
        }

    def _notice_record(
        self,
        *,
        session_id: str,
        target: DeliveryTarget,
        text: str,
        dedupe_key: str,
    ) -> dict[str, Any]:
        now = utc_now()
        return {
            "delivery_id": new_id("delivery"),
            "session_id": str(session_id),
            "audience": "group",
            "target_snapshot": target.to_snapshot(),
            "message_type": SUPPLEMENT_NOTICE_KIND,
            "projection_snapshot": {"kind": SUPPLEMENT_NOTICE_KIND},
            "rendered_parts": [text],
            "next_part_index": 0,
            "status": (
                "webui_only"
                if target.message_type == TARGET_KIND_WEBUI_ONLY
                else "pending"
            ),
            "priority": 90,
            "attempts": 0,
            "next_retry_at": now,
            "last_error_code": "",
            "last_error_message": "",
            "dedupe_key": str(dedupe_key or ""),
            "created_at": now,
            "updated_at": now,
            "delivered_at": "",
            "cancelled_at": "",
            "lease_token": "",
            "lease_until": "",
            "meta": {"kind": SUPPLEMENT_NOTICE_KIND},
            "max_attempts": 8,
        }

