from __future__ import annotations

from ...web_console_shared import *
from ...web_console_compat import (
    error_response,
    file_response,
    json_response,
    request,
    stream_response,
)




class OperationActionsMixin:
    async def _review_session_card(
        self,
        payload: Mapping[str, Any],
        *,
        actor_id: str,
    ):
        session_id = str(payload.get("session_id") or "")
        if not session_id:
            raise ValueError("缺少 session_id")
        expected_card_version = payload.get("expected_card_version")
        idempotency_key = str(payload.get("idempotency_key") or "").strip()
        if expected_card_version in {None, ""}:
            raise ValueError("缺少 expected_card_version")
        if not idempotency_key:
            raise ValueError("缺少 idempotency_key")
        item = await self.database.review_character_card(
            session_id,
            str(payload.get("participant_ref") or ""),
            bool(payload.get("approved", False)),
            actor_id,
            str(payload.get("note") or ""),
            int(expected_card_version),
            idempotency_key,
        )
        if not bool(item.get("idempotent")):
            await self.broker.publish(
                {
                    "type": "participant",
                    "action": "card_reviewed",
                    "session_id": item["session_id"],
                    "participant_id": item["id"],
                }
            )
        preflight = await self.database.opening_preflight(item["session_id"])
        instance = await self.database.get_instance_config(item["session_id"])
        world = (
            instance.get("world_snapshot")
            if isinstance(instance, Mapping)
            and isinstance(instance.get("world_snapshot"), Mapping)
            else {}
        )
        roster = await self.database.list_roster(item["session_id"])
        current = next(
            (
                row
                for row in roster
                if isinstance(row, Mapping)
                and str(row.get("id") or "") == str(item.get("id") or "")
            ),
            item,
        )
        participant = _actor_projection_row(world, current)
        return json_response({"participant": participant, "preflight": preflight})
    async def session_card_review(self):
        """Platform-session review route; requires the real session DM."""

        try:
            user = self._username()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            if not session_id:
                raise ValueError("缺少 session_id")
            await self._require_dm_capability(session_id, user)
            return await self._review_session_card(
                payload,
                actor_id=self._actor(),
            )
        except Exception as exc:
            return self._handle_error(exc)
    async def console_session_card_review(self):
        """Console-admin review route; never maps the login to a platform ID."""

        try:
            principal = self._console_principal()
            payload = await self._payload()
            actor_id = str(
                principal.get("username") or self._username()
            ).strip()
            return await self._review_session_card(
                payload,
                actor_id=f"console:{actor_id}",
            )
        except Exception as exc:
            return self._handle_error(exc)
    async def session_permission(self):
        try:
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            user = self._username()
            await self._require_dm_capability(session_id, user)
            item = await self.database.grant_permission(
                session_id,
                str(payload.get("user_id") or ""),
                str(payload.get("role") or ""),
                self._actor(),
            )
            return json_response({"permission": item})
        except Exception as exc:
            return self._handle_error(exc)
    async def session_participant(self):
        try:
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            user = self._username()
            await self._require_dm_capability(session_id, user)
            participant_ref = str(payload.get("participant_ref") or "")
            action = str(payload.get("action") or "").strip().lower()
            actor = self._actor()
            if action == "retire":
                result = await self.database.retire_participant(
                    session_id,
                    participant_ref,
                    actor,
                    forced=True,
                    reason=str(payload.get("reason") or "web_retire"),
                )
            elif action == "ban":
                duration = payload.get("duration_seconds")
                result = await self.database.create_ban(
                    session_id,
                    participant_ref,
                    actor,
                    scope=str(payload.get("scope") or "instance"),
                    duration_seconds=(
                        int(duration) if duration not in {None, ""} else None
                    ),
                    reason=str(payload.get("reason") or ""),
                )
            elif action == "unban":
                result = {
                    "revoked": await self.database.revoke_ban(
                        session_id,
                        participant_ref,
                        actor,
                    )
                }
            elif action == "designate":
                participant = await self.database.get_participant(
                    session_id,
                    participant_ref=participant_ref,
                )
                result = await self.database.designate_turn(
                    session_id,
                    participant["group_user_id"],
                    actor,
                )
            else:
                raise ValueError("不支持的参与者操作")
            await self.broker.publish(
                {
                    "type": "participant",
                    "action": action,
                    "session_id": session_id,
                }
            )
            return json_response({"result": result})
        except Exception as exc:
            return self._handle_error(exc)
    async def _supplement_viewer_role(
        self,
        session_id: str,
        user: str,
        principal: Mapping[str, Any],
    ) -> str:
        if bool(principal.get("is_admin")):
            return "admin"
        try:
            await self._require_dm_capability(session_id, user)
            return "dm"
        except Exception:
            return "player"
    async def _session_readonly(self, session_id: str) -> bool:
        try:
            session = await self.database.get_session(session_id)
            if isinstance(session, Mapping):
                return bool(session.get("readonly")) or str(
                    session.get("state") or ""
                ) == SESSION_FINISHED
        except Exception:
            return False
        return False
    async def supplements(self):
        """B/C 角色补充列表：玩家仅本人；DM/管理员见副本全部。"""
        try:
            user = self._username()
            # 该路由由 AstrBot 管理控制台的副本详情与跑团现场消费。
            # 控制台登录名不是 QQ/OpenID，不能按平台玩家身份查找参与者；
            # 旧逻辑因此把合法管理员降级为 player 并返回 403。
            principal = self._console_principal()
            session_id = str(request.query.get("session_id", "") or "").strip()
            if not session_id:
                raise ValueError("缺少 session_id：请先选择要查看的副本")
            viewer_role = await self._supplement_viewer_role(
                session_id,
                user,
                principal,
            )
            participant = None
            participant_id = ""
            if viewer_role == "player":
                participant = await self._viewer_participant_or_none(
                    session_id,
                    user,
                )
                if participant is None:
                    raise PolicyRejection(
                        "你不是该副本的成员，无法查看角色补充"
                    )
                participant_id = str(participant.get("id") or "")
            offers = await self.database.list_supplement_offers(
                session_id,
                participant_id=participant_id,
                viewer_role=viewer_role,
            )
            readonly = await self._session_readonly(session_id)
            latest_seq = 0
            try:
                latest_seq = await self.database.latest_session_event_seq(
                    session_id
                )
            except Exception:
                latest_seq = 0
            return json_response(
                {
                    "session_id": session_id,
                    "viewer_role": viewer_role,
                    "readonly": readonly,
                    "items": _project_supplement_offers(
                        offers,
                        viewer_role=viewer_role,
                        participant=participant,
                        readonly=readonly,
                    ),
                    "latest_seq": latest_seq,
                }
            )
        except Exception as exc:
            return self._handle_error(exc)
    async def supplement_action(self):
        """玩家按当前页候选序号确认/暂缓/取消/更换 B/C 补充。

        主持人与管理员只能查看状态，不能代替玩家确认秘密内容；
        玩家回复只需页内序号，绝不要求输入内部稳定 ID。
        """
        try:
            user = self._username()
            principal = self._web_principal()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "").strip()
            if not session_id:
                raise ValueError("缺少 session_id：请先选择要查看的副本")
            offer_ref = str(payload.get("offer_ref") or "").strip()
            if not offer_ref:
                raise ValueError("缺少要处理的补充项目，请刷新面板后重试")
            action = str(payload.get("action") or "").strip()
            if action not in {"confirm", "postpone", "cancel", "reject"}:
                raise ValueError(
                    "未知的角色补充操作；"
                    "支持：confirm / postpone / cancel / reject"
                )
            await self._supplement_viewer_role(session_id, user, principal)
            participant = await self._viewer_participant_or_none(
                session_id,
                user,
            )
            if participant is None:
                raise PolicyRejection(
                    "你不是该副本的成员，无法操作角色补充"
                )
            expected_revision = payload.get("expected_revision")
            idempotency_key = str(
                payload.get("idempotency_key") or ""
            ).strip()
            if expected_revision in {None, ""}:
                raise ValueError("缺少 expected_revision")
            if not idempotency_key:
                raise ValueError("缺少 idempotency_key")
            raw_indexes = payload.get("candidate_indexes") or []
            if not isinstance(raw_indexes, list) or any(
                not str(item or "").strip().isdigit()
                or int(str(item).strip()) <= 0
                for item in raw_indexes
            ):
                raise ValueError(
                    "候选序号无效，请重新加载角色补充后重试"
                )
            indexes = [int(str(item).strip()) for item in raw_indexes]
            text_value = str(payload.get("text_value") or "").strip()
            if action in {"confirm", "reject"}:
                if not indexes:
                    if action == "confirm" and text_value:
                        pass
                    else:
                        raise ValueError(
                            "请选择候选项序号"
                            if action == "confirm"
                            else "请选择要更换的候选项"
                        )
            private_origin = str(
                (participant or {}).get("private_origin") or ""
            ).strip()
            if not private_origin:
                raise ValueError(
                    "该角色尚未通过私聊绑定，无法在面板确认。"
                    "请本人私聊 BOT 发送：/团 当前"
                )
            if await self._session_readonly(session_id):
                raise ValueError("副本已归档，角色补充只读")
            if action == "confirm":
                result = await self.database.confirm_supplement_offer(
                    session_id,
                    offer_ref,
                    candidate_indexes=indexes or None,
                    text_value=text_value,
                    actor=user,
                    private_origin=private_origin,
                    expected_revision=int(expected_revision),
                    idempotency_key=idempotency_key,
                )
            elif action == "postpone":
                result = await self.database.postpone_supplement_offer(
                    session_id,
                    offer_ref,
                    actor=user,
                    private_origin=private_origin,
                    expected_revision=int(expected_revision),
                    idempotency_key=idempotency_key,
                )
            elif action == "cancel":
                result = await self.database.cancel_supplement_offer(
                    session_id,
                    offer_ref,
                    actor=user,
                    private_origin=private_origin,
                    expected_revision=int(expected_revision),
                    idempotency_key=idempotency_key,
                )
            else:
                result = await self.database.reject_supplement_offer(
                    session_id,
                    offer_ref,
                    candidate_indexes=indexes,
                    actor=user,
                    private_origin=private_origin,
                    expected_revision=int(expected_revision),
                    idempotency_key=idempotency_key,
                )
            return json_response(
                {
                    "ok": True,
                    "action": action,
                    "message": _supplement_action_message(action, result),
                    "result": {
                        "field_label": str(
                            result.get("field_label")
                            or "角色资料"
                        ),
                        "state": str(result.get("state") or action),
                    },
                }
            )
        except Exception as exc:
            return self._handle_error(exc)
    async def players(self):
        try:
            self._username()
            session_id = str(request.query.get("session_id", "") or "")
            return json_response(
                {"items": await self.database.list_players(session_id)}
            )
        except Exception as exc:
            return self._handle_error(exc)
    async def player_save(self):
        try:
            self._require_admin()
            item = await self.database.save_player(
                await self._payload(),
                self._actor(),
            )
            return json_response({"item": item})
        except Exception as exc:
            return self._handle_error(exc)
    async def player_delete(self):
        try:
            self._require_admin()
            payload = await self._payload()
            await self.database.delete_player(
                str(payload.get("id", "")),
                self._actor(),
            )
            return json_response({"deleted": True})
        except Exception as exc:
            return self._handle_error(exc)
    async def memories(self):
        try:
            self._username()
            session_id = str(request.query.get("session_id", "") or "")
            query = str(request.query.get("q", "") or "")
            limit = request.query.get("limit", 100, type=int)
            return json_response(
                {
                    "items": await self.database.list_memories(
                        session_id,
                        query,
                        limit,
                    )
                }
            )
        except Exception as exc:
            return self._handle_error(exc)
    async def memory_save(self):
        try:
            self._require_admin()
            item = await self.database.save_memory(
                await self._payload(),
                self._actor(),
            )
            return json_response({"item": item})
        except Exception as exc:
            return self._handle_error(exc)
    async def memory_delete(self):
        try:
            self._require_admin()
            payload = await self._payload()
            await self.database.delete_memory(
                str(payload.get("id", "")),
                self._actor(),
            )
            return json_response({"deleted": True})
        except Exception as exc:
            return self._handle_error(exc)
    async def snapshots(self):
        try:
            self._username()
            session_id = str(request.query.get("session_id", "") or "")
            return json_response(
                {"items": await self.database.list_snapshots(session_id)}
            )
        except Exception as exc:
            return self._handle_error(exc)
    async def snapshot_create(self):
        try:
            user = self._username()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            if not session_id:
                raise ValueError("缺少 session_id")
            await self._require_dm_capability(session_id, user)
            expected_revision = payload.get("expected_revision")
            idempotency_key = self._request_idempotency_key(payload)
            if expected_revision in {None, ""}:
                raise ValueError("缺少 expected_revision")
            if not idempotency_key:
                raise ValueError("缺少 idempotency_key")
            replace = bool(payload.get("replace", False))
            result = await self.database.create_snapshot(
                session_id,
                str(payload.get("name") or ""),
                self._actor(),
                replace=replace,
                expected_revision=int(expected_revision),
                expected_snapshot_revision=(
                    int(payload["expected_snapshot_revision"])
                    if replace
                    and payload.get("expected_snapshot_revision") not in {None, ""}
                    else None
                ),
                idempotency_key=idempotency_key,
            )
            return json_response({"item": result["snapshot"], "operation": result})
        except Exception as exc:
            return self._handle_error(exc)
    async def snapshot_restore(self):
        try:
            user = self._username()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            if not session_id:
                raise ValueError("缺少 session_id")
            await self._require_dm_capability(session_id, user)
            expected_revision = payload.get("expected_revision")
            idempotency_key = self._request_idempotency_key(payload)
            if expected_revision in {None, ""}:
                raise ValueError("缺少 expected_revision")
            if not idempotency_key:
                raise ValueError("缺少 idempotency_key")
            result = await self.database.restore_snapshot(
                session_id,
                str(payload.get("snapshot_ref") or ""),
                self._actor(),
                expected_revision=int(expected_revision),
                idempotency_key=idempotency_key,
            )
            await self.database.pause_session_timers(
                session_id,
                self._actor(),
            )
            await self.broker.publish(
                {
                    "type": "session",
                    "action": "restore",
                    "session_id": session_id,
                }
            )
            return json_response({
                "session": await self.database.get_session(session_id),
                "operation": result,
            })
        except Exception as exc:
            return self._handle_error(exc)
