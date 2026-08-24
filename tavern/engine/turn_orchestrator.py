from .shared import *
from .errors import *
from .turn_state import TurnProcessState
from ..copy.entities import decorate_entity


class TurnOrchestratorMixin:
    async def process(
        self,
        *,
        event: Any,
        session_id: str,
        sender_id: str,
        sender_name: str,
        content: str,
        workflow: Mapping[str, Any] | None = None,
        progress: ProgressCallback | None = None,
        operator_id: str = "",
        force_actor: bool = False,
        item_ops: Sequence[Mapping[str, Any]] | None = None,
        operation_id_override: str = "",
        operation_request_override: Mapping[str, Any] | None = None,
        actor_context: Mapping[str, Any] | None = None,
    ) -> EngineReply:
        config = self.config_provider()
        text = clean_text(content, max_chars=config.max_input_chars)
        if not text:
            raise TavernEngineError("行动内容为空")

        preflight_session = await self.database.get_session(session_id)
        if preflight_session["state"] != "running":
            raise TavernEngineError("酒馆当前不在运行状态")
        preflight_turn = await self.database.get_turn_status(session_id)
        if (
            not force_actor
            and preflight_turn["current_user_id"]
            and preflight_turn["current_user_id"] != sender_id
        ):
            current_name = str(
                preflight_turn.get("current_name") or ""
            ).strip()
            if not current_name:
                raise TavernTurnOrderError(
                    "当前行动者缺少可公开显示的角色名称，"
                    "系统无法安全确认行动顺序；本条内容未记录。",
                    turn=preflight_turn,
                )
            current = decorate_entity("character", current_name)
            members = {
                str(item.get("user_id") or "")
                for item in preflight_turn["order"]
            }
            join_note = (
                "你尚未加入队列，请先发送 /团 加入；"
                if sender_id not in members
                else ""
            )
            raise TavernTurnOrderError(
                f"{join_note}当前轮到 {current}，本条内容未记录。",
                turn=preflight_turn,
            )

        lock = await self._session_lock(session_id)
        async with lock:
            prepared = await self._prepare_turn_process_state(
                event=event,
                session_id=session_id,
                sender_id=sender_id,
                sender_name=sender_name,
                workflow=workflow,
                progress=progress,
                operator_id=operator_id,
                force_actor=force_actor,
                item_ops=item_ops,
                operation_id_override=operation_id_override,
                operation_request_override=operation_request_override,
                actor_context=actor_context,
                config=config,
                text=text,
            )
            if isinstance(prepared, EngineReply):
                return prepared
            await self._run_turn_generation(prepared)
            await self._validate_turn_result(prepared)
            await self._commit_turn_result(prepared)
            return await self._deliver_turn_result(prepared)
