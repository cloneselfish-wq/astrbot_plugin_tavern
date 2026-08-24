from .shared import *
from .errors import *

class ParticipationMixin:
    async def join_player(
        self,
        *,
        session_id: str,
        sender_id: str,
        sender_name: str,
    ) -> dict[str, Any]:
        lock = await self._session_lock(session_id)
        async with lock:
            session = await self.database.get_session(session_id)
            if session["state"] in {"closed", "maintenance"}:
                raise TavernEngineError("酒馆当前不接受玩家加入")
            return await self.database.join_turn_order(
                session_id,
                sender_id,
                sender_name,
                sender_id,
            )

    async def leave_player(
        self,
        *,
        session_id: str,
        sender_id: str,
    ) -> dict[str, Any]:
        lock = await self._session_lock(session_id)
        async with lock:
            return await self.database.leave_turn_order(
                session_id,
                sender_id,
                sender_id,
            )

    async def skip_player(
        self,
        *,
        session_id: str,
        sender_id: str,
        force: bool = False,
        controlled_user_id: str = "",
    ) -> dict[str, Any]:
        lock = await self._session_lock(session_id)
        async with lock:
            session = await self.database.get_session(session_id)
            if session["state"] != "running":
                raise TavernEngineError("酒馆当前不在运行状态")
            target_user_id = controlled_user_id or sender_id
            if target_user_id != sender_id and not force:
                participant = await self.database.get_participant(
                    session_id,
                    user_id=target_user_id,
                )
                control = (
                    await self.database.authorize_participant_control(
                        session_id,
                        participant["id"],
                        sender_id,
                        "skip",
                    )
                )
                if not control["authorized"]:
                    raise TavernEngineError("你没有该角色的有效代控授权")
            return await self.database.skip_turn(
                session_id,
                target_user_id,
                sender_id,
                force=force,
            )

    async def process_choice(
        self,
        *,
        event: Any,
        session_id: str,
        sender_id: str,
        sender_name: str,
        choice_key: str,
        flavor_text: str = "",
        inspiration_mode: str = "",
        progress: ProgressCallback | None = None,
        operator_id: str = "",
        force: bool = False,
    ) -> EngineReply:
        choice_set = await self.database.active_choice_set(session_id)
        if not choice_set:
            vote = await self.database.active_vote(session_id)
            if vote:
                raise TavernEngineError(
                    "当前处于集体投票阶段，请使用 /团 投票 A"
                )
            raise TavernEngineError("当前没有可选择的行动选项")
        participant = choice_set.get("participant")
        if not participant:
            raise TavernEngineError("当前选项没有有效的行动角色")
        control = await self.database.authorize_participant_control(
            session_id,
            participant["id"],
            sender_id,
            "choose",
        )
        if not control["authorized"] and not force:
            owner = (
                participant.get("character_name")
                or participant.get("display_name")
                or participant.get("group_user_id")
            )
            raise TavernTurnOrderError(
                f"当前选项属于 {owner}，本条内容未记录。",
                turn=await self.database.get_turn_status(session_id),
            )
        if force:
            control = {
                "authorized": True,
                "mode": "admin_forced",
                "controller_user_id": sender_id,
                "source": "admin",
                "forced": True,
            }
        key = str(choice_key or "").strip().upper()
        selected = next(
            (
                item
                for item in choice_set["choices"]
                if str(item.get("key") or "").upper() == key
            ),
            None,
        )
        if not selected:
            raise TavernEngineError("请选择 A、B、C 或 D")
        inspiration_mode = str(inspiration_mode or "").strip().lower()
        if inspiration_mode not in {"", "advantage", "reroll"}:
            raise TavernEngineError("灵感用法必须为优势或重投")
        if inspiration_mode and not selected.get("requires_check"):
            raise TavernEngineError("该选项不需要检定，不能消耗灵感点")
        flavor = clean_text(flavor_text, max_chars=160)
        acting_user_id = str(participant["group_user_id"])
        if bool(selected.get("collective")):
            # 0.11.2：全队行动选项 → 由引擎直接发起集体表决，
            # 不再依赖叙事模型自行生成 group_decision（模型未生成时
            # 旧逻辑会把整轮判为“未提交”，玩家陷入死胡同）。
            return await self._start_team_vote(
                session_id=session_id,
                participant=participant,
                selected=selected,
                sender_id=sender_id,
                flavor=flavor,
            )
        content = f"选择 {key}：{selected['text']}"
        if flavor:
            content += f"\n演绎偏好：{flavor}"
        return await self.process(
            event=event,
            session_id=session_id,
            sender_id=acting_user_id,
            sender_name=(
                participant.get("character_name")
                or participant.get("display_name")
                or sender_name
            ),
            content=content,
            workflow={
                "choice_set_id": choice_set["id"],
                "selected_key": key,
                "flavor_text": flavor,
                "requires_check": bool(selected.get("requires_check")),
                "collective": bool(selected.get("collective")),
                "selected_choice": dict(selected),
                "inspiration_mode": inspiration_mode,
                "controller_user_id": sender_id,
                "control_mode": control["mode"],
                "control_source": control.get("source", ""),
            },
            progress=progress,
            operator_id=operator_id,
            force_actor=force,
        )

    async def process_choice_command(
        self,
        command: ChoiceCommand,
        *,
        event: Any = None,
        progress: ProgressCallback | None = None,
    ) -> EngineReply:
        """Submit human and AI choices through one resolution/commit entry."""
        choice_set = await self.database.active_choice_set(command.session_id)
        if not choice_set or str(choice_set.get("id") or "") != command.choice_set_id:
            raise TavernEngineError("当前选项已经失效，请重新读取本轮")
        if int(choice_set.get("session_revision") or 0) != int(
            command.expected_session_revision
        ):
            raise TavernEngineError("副本状态已更新，旧选项没有提交")
        actor = choice_set.get("actor") or {}
        actor_ref = str(actor.get("actor_ref") or "")
        if actor_ref:
            if actor_ref != str(command.actor_ref or ""):
                raise TavernEngineError("该选项不属于当前 AI 队友")
            key = str(command.choice_key or "").strip().upper()
            selected = next(
                (
                    item
                    for item in choice_set.get("choices") or []
                    if str(item.get("key") or "").upper() == key
                ),
                None,
            )
            if selected is None:
                raise TavernEngineError("AI 队友选择了无效选项")
            if bool(selected.get("collective")):
                return await self._start_team_vote(
                    session_id=command.session_id,
                    participant=actor,
                    selected=selected,
                    sender_id=actor_ref,
                    flavor=command.flavor_text,
                )
            return await self.process(
                event=event,
                session_id=command.session_id,
                sender_id=actor_ref,
                sender_name=str(actor.get("display_name") or "AI 队友"),
                content=f"选择 {key}：{selected.get('text')}",
                workflow={
                    "choice_set_id": command.choice_set_id,
                    "selected_key": key,
                    "flavor_text": clean_text(
                        command.flavor_text,
                        max_chars=160,
                    ),
                    "requires_check": bool(selected.get("requires_check")),
                    "collective": bool(selected.get("collective")),
                    "selected_choice": dict(selected),
                    "controller_user_id": actor_ref,
                    "control_mode": "ai_policy",
                    "control_source": "ai_companion",
                },
                progress=progress,
                force_actor=True,
                actor_context=actor,
                operation_id_override=command.idempotency_key,
                operation_request_override={
                    "turn_no": int(choice_set.get("round_no") or 0),
                    "actor_id": actor_ref,
                    "choice_set_id": command.choice_set_id,
                    "selected_key": key,
                    "idempotency_key": command.idempotency_key,
                },
            )
        participant = choice_set.get("participant")
        if not participant:
            raise TavernEngineError("当前选项没有有效的行动角色")
        return await self.process_choice(
            event=event,
            session_id=command.session_id,
            sender_id=str(participant.get("group_user_id") or ""),
            sender_name=str(
                participant.get("character_name")
                or participant.get("display_name")
                or ""
            ),
            choice_key=command.choice_key,
            flavor_text=command.flavor_text,
        )
