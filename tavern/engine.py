from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from typing import Any

from .config import TavernConfig
from .database import (
    DatabaseConflictError,
    InvalidTransitionError,
    TavernDatabase,
)
from .events import EventBroker
from .lifecycle import (
    fallback_choices,
    format_choices,
    normalize_choices,
)
from .prompts import (
    checked_resolution_prompt,
    planning_prompt,
    repair_prompt,
    system_prompt,
)
from .resolution import (
    CheckRequest,
    DiceResult,
    Resolution,
    apply_state_patch,
    extract_json_object,
    roll_check,
    roll_group_check,
    roll_opposed_check,
    validate_resolution,
)
from .security import RateLimiter, clean_text


class TavernEngineError(RuntimeError):
    pass


class TavernBusyError(TavernEngineError):
    pass


class TavernPlayerDisabledError(TavernEngineError):
    pass


class TavernTurnOrderError(TavernEngineError):
    def __init__(
        self,
        message: str,
        *,
        turn: Mapping[str, Any],
        joined: bool = False,
    ) -> None:
        super().__init__(message)
        self.turn = dict(turn)
        self.joined = bool(joined)


@dataclass(frozen=True, slots=True)
class EngineReply:
    text: str
    session: dict[str, Any]
    dice: DiceResult | None = None
    ooc: bool = False
    turn: dict[str, Any] | None = None


class TavernEngine:
    def __init__(
        self,
        *,
        context: Any,
        database: TavernDatabase,
        config_provider: Callable[[], TavernConfig],
        broker: EventBroker,
    ) -> None:
        self.context = context
        self.database = database
        self.config_provider = config_provider
        self.broker = broker
        self.rate_limiter = RateLimiter()
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def _session_lock(self, session_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[session_id] = lock
            return lock

    @staticmethod
    def _provider_order(
        primary: str,
        fallbacks: tuple[str, ...],
    ) -> list[str]:
        result: list[str] = []
        for provider_id in (primary, *fallbacks):
            normalized = str(provider_id or "").strip()
            if normalized and normalized not in result:
                result.append(normalized)
        return result

    @staticmethod
    def _check_request_from_payload(
        payload: Mapping[str, Any],
    ) -> CheckRequest:
        return CheckRequest(
            stat=str(payload.get("stat") or "通用"),
            reason=str(payload.get("reason") or "行动存在不确定性"),
            difficulty=int(payload.get("difficulty") or 12),
            modifier=int(payload.get("modifier") or 0),
            risk=str(payload.get("risk") or "controlled"),
            check_type=str(payload.get("check_type") or "standard"),
            advantage_sources=tuple(
                str(item)
                for item in payload.get("advantage_sources", [])
            ),
            disadvantage_sources=tuple(
                str(item)
                for item in payload.get("disadvantage_sources", [])
            ),
            known_consequences=str(
                payload.get("known_consequences") or ""
            ),
            visibility=str(payload.get("visibility") or "public"),
            inspiration_mode=str(
                payload.get("inspiration_mode") or ""
            ),
            participant_ids=tuple(
                str(item)
                for item in payload.get("participant_ids", [])
            ),
            opponent_modifier=int(
                payload.get("opponent_modifier") or 0
            ),
        )

    @classmethod
    def _check_request_from_locked_choice(
        cls,
        workflow: Mapping[str, Any],
    ) -> CheckRequest:
        selected_choice = (
            dict(workflow.get("selected_choice") or {})
            if isinstance(workflow.get("selected_choice"), Mapping)
            else {}
        )
        return cls._check_request_from_payload(
            {
                "stat": selected_choice.get("check_stat") or "通用",
                "reason": (
                    selected_choice.get("text")
                    or "该选项已被标记为必须检定"
                ),
                "difficulty": selected_choice.get("difficulty") or 12,
                "modifier": 0,
                "risk": selected_choice.get("risk") or "controlled",
                "check_type": (
                    selected_choice.get("check_type") or "standard"
                ),
                "advantage_sources": (
                    selected_choice.get("advantage_sources") or []
                ),
                "disadvantage_sources": (
                    selected_choice.get("disadvantage_sources") or []
                ),
                "known_consequences": (
                    selected_choice.get("known_consequences") or ""
                ),
                "inspiration_mode": (
                    workflow.get("inspiration_mode") or ""
                ),
            }
        )

    @staticmethod
    def _dice_result_from_payload(
        payload: Mapping[str, Any],
    ) -> DiceResult:
        return DiceResult(
            die=int(payload.get("die") or 0),
            modifier=int(payload.get("modifier") or 0),
            total=int(payload.get("total") or 0),
            difficulty=int(payload.get("difficulty") or 0),
            outcome=str(payload.get("outcome") or "failure"),
            critical=(
                str(payload["critical"])
                if payload.get("critical")
                else None
            ),
            rolls=tuple(int(item) for item in payload.get("rolls", [])),
            kept=int(payload.get("kept") or 0),
            dice_mode=str(payload.get("dice_mode") or "standard"),
            margin=int(payload.get("margin") or 0),
            risk=str(payload.get("risk") or "controlled"),
            check_type=str(payload.get("check_type") or "standard"),
            advantage_sources=tuple(
                str(item)
                for item in payload.get("advantage_sources", [])
            ),
            disadvantage_sources=tuple(
                str(item)
                for item in payload.get("disadvantage_sources", [])
            ),
            advantages_cancelled=bool(
                payload.get("advantages_cancelled", False)
            ),
            original_rolls=tuple(
                int(item) for item in payload.get("original_rolls", [])
            ),
            rerolled=bool(payload.get("rerolled", False)),
            visibility=str(payload.get("visibility") or "public"),
            members=tuple(
                dict(item)
                for item in payload.get("members", [])
                if isinstance(item, Mapping)
            ),
        )

    @staticmethod
    def _format_dice_result(
        dice: DiceResult,
        stat: str,
    ) -> str:
        labels = {
            "critical_success": "大成功",
            "success": "成功",
            "success_with_cost": "代价成功",
            "failure": "失败",
            "critical_failure": "大失败",
        }
        mode_labels = {
            "standard": "常规",
            "advantage": "优势",
            "disadvantage": "劣势",
            "group": "集体",
        }
        result_label = labels.get(dice.outcome, dice.outcome)
        if dice.visibility == "hidden":
            return ""
        if dice.members and dice.check_type in {"group", "resistance"}:
            member_lines = [
                (
                    f"- {item.get('name') or item.get('actor_id')}: "
                    f"{item.get('rolls')} "
                    f"{int(item.get('modifier') or 0):+d} → "
                    f"{item.get('total')} · "
                    f"{labels.get(str(item.get('outcome')), item.get('outcome'))}"
                )
                for item in dice.members
            ]
            header = (
                f"【{stat}·{mode_labels.get(dice.dice_mode, dice.dice_mode)}"
                f"检定】{dice.total}/{dice.difficulty} 人达标 · "
                f"{result_label}"
            )
            return "\n".join([header, *member_lines])
        rolls = list(dice.rolls)
        pool = (
            f"{rolls} → 取 {dice.kept}"
            if len(rolls) > 1
            else str(dice.kept)
        )
        modifier = f"{dice.modifier:+d}"
        header = (
            f"【{stat}检定】"
            f"［{mode_labels.get(dice.dice_mode, dice.dice_mode)}］"
            f"{pool} {modifier} → {dice.total}"
        )
        if dice.visibility == "public":
            header += f" / DC {dice.difficulty}"
        header += f" · {result_label}"
        if dice.visibility == "public":
            source_lines = []
            if dice.advantage_sources:
                source_lines.append(
                    "优势：" + "；".join(dice.advantage_sources)
                )
            if dice.disadvantage_sources:
                source_lines.append(
                    "劣势：" + "；".join(dice.disadvantage_sources)
                )
            if dice.advantages_cancelled:
                source_lines.append("优劣势同时存在，本次互相抵消")
            if dice.rerolled:
                source_lines.append(
                    f"灵感重投：原骰池 {list(dice.original_rolls)}"
                )
            if dice.check_type == "opposed" and dice.members:
                defender = dice.members[0]
                source_lines.append(
                    "对抗方："
                    f"{defender.get('name') or '防守方'} "
                    f"{defender.get('rolls')} "
                    f"{int(defender.get('modifier') or 0):+d} → "
                    f"{defender.get('total')}"
                )
            if source_lines:
                header += "\n" + "\n".join(source_lines)
        return header

    async def _story_providers(
        self,
        event: Any,
        config: TavernConfig,
    ) -> list[str]:
        primary = config.provider_id
        current_error: Exception | None = None
        if not primary:
            try:
                primary = await self.context.get_current_chat_provider_id(
                    umo=event.unified_msg_origin
                )
            except Exception as exc:
                current_error = exc
        providers = self._provider_order(
            primary,
            config.fallback_provider_ids,
        )
        if providers:
            return await self.database.filter_healthy_providers(providers)
        if current_error:
            raise TavernEngineError(
                "无法取得当前群会话模型，且没有配置备用模型"
            ) from current_error
        raise TavernEngineError("没有可用的叙事模型")

    @staticmethod
    def _component_children(component: Any) -> list[Any]:
        for attribute in ("chain", "message", "message_chain"):
            value = getattr(component, attribute, None)
            if isinstance(value, (list, tuple)):
                return list(value)
            nested = getattr(value, "chain", None)
            if isinstance(nested, (list, tuple)):
                return list(nested)
        return []

    async def _image_references(
        self,
        event: Any,
        limit: int,
    ) -> list[str]:
        message_obj = getattr(event, "message_obj", None)
        message = getattr(message_obj, "message", None)
        if isinstance(message, (list, tuple)):
            pending = list(message)
        elif message is None:
            pending = []
        else:
            pending = self._component_children(message)
            if not pending:
                try:
                    pending = list(message)
                except TypeError:
                    pending = [message]
        result: list[str] = []
        seen_components: set[int] = set()
        while pending and len(result) < limit:
            component = pending.pop(0)
            identity = id(component)
            if identity in seen_components:
                continue
            seen_components.add(identity)
            pending.extend(self._component_children(component))
            if component.__class__.__name__.casefold() != "image":
                continue
            reference = str(
                getattr(component, "url", "")
                or getattr(component, "file", "")
                or ""
            ).strip()
            if not reference:
                converter = getattr(component, "convert_to_base64", None)
                if callable(converter):
                    try:
                        converted = converter()
                        if inspect.isawaitable(converted):
                            converted = await converted
                    except Exception as exc:
                        raise TavernEngineError(
                            "无法读取消息中的图片，本条内容未记录"
                        ) from exc
                    encoded = str(converted or "").strip()
                    if encoded:
                        if encoded.startswith(("data:", "base64://")):
                            reference = encoded
                        else:
                            reference = (
                                "data:image/jpeg;base64," + encoded
                            )
            if not reference:
                raise TavernEngineError(
                    "无法读取消息中的图片，本条内容未记录"
                )
            if reference and reference not in result:
                result.append(reference)
        return result

    async def _caption_images(
        self,
        *,
        event: Any,
        config: TavernConfig,
    ) -> str:
        image_urls = await self._image_references(
            event,
            config.max_images_per_turn,
        )
        if not image_urls:
            return ""
        if not config.image_caption_provider_id:
            raise TavernEngineError(
                "检测到图片，但尚未配置图片转述模型；"
                "本条内容未记录。"
            )
        try:
            response = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=config.image_caption_provider_id,
                    prompt=(
                        f"{config.image_caption_prompt}\n\n"
                        f"共 {len(image_urls)} 张图片，请按“图1、图2……”"
                        "分别描述。"
                    ),
                    image_urls=image_urls,
                    temperature=0.1,
                    max_tokens=min(1200, config.max_tokens),
                ),
                timeout=config.request_timeout_seconds,
            )
        except TimeoutError as exc:
            raise TavernEngineError(
                "图片转述模型请求超时，本条内容未记录"
            ) from exc
        except Exception as exc:
            raise TavernEngineError(
                f"图片转述模型调用失败：{type(exc).__name__}"
            ) from exc
        caption = clean_text(
            getattr(response, "completion_text", ""),
            max_chars=min(6000, config.max_output_chars),
        )
        if not caption:
            raise TavernEngineError("图片转述模型没有返回有效描述")
        return caption

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
    ) -> EngineReply:
        choice_set = await self.database.active_choice_set(session_id)
        if not choice_set:
            vote = await self.database.active_vote(session_id)
            if vote:
                raise TavernEngineError(
                    "当前处于集体投票阶段，请使用 /酒馆 投票 A"
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
        if not control["authorized"]:
            owner = (
                participant.get("character_name")
                or participant.get("display_name")
                or participant.get("group_user_id")
            )
            raise TavernTurnOrderError(
                f"当前选项属于 {owner}，本条内容未记录。",
                turn=await self.database.get_turn_status(session_id),
            )
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
        content = f"选择 {key}：{selected['text']}"
        if flavor:
            content += f"\n演绎偏好：{flavor}"
        acting_user_id = str(participant["group_user_id"])
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
            },
        )

    async def reroll_choices(
        self,
        *,
        event: Any,
        session_id: str,
        sender_id: str,
    ) -> dict[str, Any]:
        choice_set = await self.database.active_choice_set(session_id)
        if not choice_set or not choice_set.get("participant"):
            raise TavernEngineError("当前没有可重整的个人行动选项")
        participant = choice_set["participant"]
        control = await self.database.authorize_participant_control(
            session_id,
            participant["id"],
            sender_id,
            "reroll",
        )
        if not control["authorized"]:
            raise TavernEngineError("只能重整自己当前回合的选项")
        if int(choice_set["reroll_count"]) >= 1:
            raise TavernEngineError("本回合的免费重整次数已经用完")
        config = self.config_provider()
        session = await self.database.get_session(session_id)
        roster = await self.database.list_roster(session_id)
        rich_participant = next(
            (
                item
                for item in roster
                if item.get("id") == participant.get("id")
            ),
            None,
        )
        if rich_participant:
            participant = rich_participant
        try:
            instance = await self.database.get_instance_config(session_id)
            world = dict(instance["world_snapshot"])
        except Exception:
            world = await self.database.get_world(session["world_id"])
        events = await self.database.recent_events(
            session_id,
            config.recent_turns * 2 + 6,
        )
        providers = await self._story_providers(event, config)
        choices = await self._generate_choices(
            provider_ids=providers,
            world=world,
            session=session,
            participant=participant,
            events=events,
            config=config,
            avoid=choice_set["choices"],
        )
        result = await self.database.replace_active_choices(
            session_id,
            participant["id"],
            choices,
            actor_id=sender_id,
        )
        result["participant"] = participant
        return result

    async def process(
        self,
        *,
        event: Any,
        session_id: str,
        sender_id: str,
        sender_name: str,
        content: str,
        workflow: Mapping[str, Any] | None = None,
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
            preflight_turn["current_user_id"]
            and preflight_turn["current_user_id"] != sender_id
        ):
            current = (
                preflight_turn["current_name"]
                or preflight_turn["current_user_id"]
            )
            members = {
                str(item.get("user_id") or "")
                for item in preflight_turn["order"]
            }
            join_note = (
                "你尚未加入队列，请先发送 /酒馆 加入；"
                if sender_id not in members
                else ""
            )
            raise TavernTurnOrderError(
                f"{join_note}当前轮到 {current}，本条内容未记录。",
                turn=preflight_turn,
            )

        lock = await self._session_lock(session_id)
        async with lock:
            session = await self.database.get_session(session_id)
            if session["state"] != "running":
                raise TavernEngineError("酒馆当前不在运行状态")

            remaining = self.rate_limiter.remaining(
                session_id,
                sender_id,
                config.user_cooldown_seconds,
            )
            if remaining > 0:
                raise TavernBusyError(
                    f"行动提交过快，请等待 {remaining:.1f} 秒"
                )

            try:
                joined_result = await self.database.join_turn_order(
                    session_id,
                    sender_id,
                    sender_name,
                    sender_id,
                )
            except InvalidTransitionError as exc:
                if "玩家身份" in str(exc):
                    raise TavernPlayerDisabledError(str(exc)) from exc
                raise
            player = joined_result["player"]
            if not player["enabled"]:
                raise TavernPlayerDisabledError("该玩家已被停用")
            session = joined_result["session"]
            turn = joined_result["turn"]
            if turn["current_user_id"] != sender_id:
                current = turn["current_name"] or turn["current_user_id"]
                joined_note = "你已加入队尾；" if joined_result["joined"] else ""
                raise TavernTurnOrderError(
                    f"{joined_note}当前轮到 {current}，本条内容未记录。",
                    turn=turn,
                    joined=joined_result["joined"],
                )
            acting_round = int(turn["round_no"])

            for prefix in config.ooc_prefixes:
                if text.lower().startswith(prefix.lower()):
                    await self.database.append_ooc(
                        session_id,
                        sender_id,
                        sender_name,
                        text,
                    )
                    await self.broker.publish(
                        {
                            "type": "ooc",
                            "session_id": session_id,
                            "actor": sender_name,
                        }
                    )
                    return EngineReply(
                        text=(
                            "【OOC】场外发言已记录，"
                            "本轮世界状态与行动顺序均未推进。"
                        ),
                        session=session,
                        ooc=True,
                        turn=turn,
                    )

            image_caption = await self._caption_images(
                event=event,
                config=config,
            )
            player_input = text
            if image_caption:
                player_input = (
                    f"{text}\n\n"
                    "<image_descriptions>\n"
                    f"{image_caption}\n"
                    "</image_descriptions>"
                )

            current_world = await self.database.get_world(session["world_id"])
            try:
                instance = await self.database.get_instance_config(session_id)
                world = dict(instance["world_snapshot"])
                world.setdefault(
                    "characters",
                    current_world.get("characters", []),
                )
            except Exception:
                world = current_world
            players = await self.database.list_players(session_id)
            roster = await self.database.list_roster(session_id)
            acting_participant = next(
                (
                    item
                    for item in roster
                    if str(item.get("group_user_id") or "") == sender_id
                ),
                None,
            )
            player = dict(player)
            if acting_participant:
                player.update(
                    {
                        "participant_id": acting_participant.get("id"),
                        "character_name": (
                            acting_participant.get("character_name")
                            or player.get("character_name")
                        ),
                        "character_code": acting_participant.get(
                            "character_code"
                        ),
                        "profile": acting_participant.get(
                            "card_profile",
                            {},
                        ),
                        "stats": acting_participant.get("card_stats", {}),
                        "runtime_state": acting_participant.get(
                            "runtime_state",
                            {},
                        ),
                        "participation_status": acting_participant.get(
                            "participation_status"
                        ),
                    }
                )
            session = dict(session)
            session["players"] = players
            session["roster"] = roster
            session["turn_status"] = turn
            session["return_requests"] = await self.database.list_return_requests(
                session_id
            )
            rule_state = await self.database.get_session_rule_state(session_id)
            context_budget = dict(rule_state.get("context_budget") or {})
            recent_turn_limit = max(
                2,
                min(
                    50,
                    int(
                        context_budget.get(
                            "recent_turns",
                            config.recent_turns,
                        )
                    ),
                ),
            )
            memory_limit = max(
                0,
                min(
                    40,
                    int(
                        context_budget.get(
                            "memories",
                            config.memory_limit,
                        )
                    ),
                ),
            )
            events = await self.database.recent_events(
                session_id,
                recent_turn_limit * 2 + 6,
            )
            memories = await self.database.list_memories(
                session_id,
                player_input,
                memory_limit,
            )
            session["session_characters"] = (
                await self.database.list_session_characters(
                    session_id,
                    include_archived=False,
                    context_only=True,
                )
            )
            ledger_limit = max(
                0,
                min(100, int(context_budget.get("ledger_items", 16))),
            )
            session["story_ledger"] = (
                await self.database.list_story_ledger(session_id)
            )[:ledger_limit]
            session["scene_clocks"] = await self.database.list_scene_clocks(
                session_id
            )
            session["content_boundaries"] = rule_state.get(
                "content_boundaries",
                {},
            )
            session["progress"] = rule_state.get("progress", {})
            session["recovery"] = rule_state.get("recovery", {})
            provider_ids = await self._story_providers(event, config)

            system = system_prompt(world)
            first_prompt = planning_prompt(
                session=session,
                player=player,
                player_input=player_input,
                events=events,
                memories=memories,
                allow_checks=config.two_phase_checks,
                workflow=workflow,
            )
            resolution, used_provider_id = await self._generate_resolution(
                provider_ids=provider_ids,
                system=system,
                prompt=first_prompt,
                config=config,
            )

            dice: DiceResult | None = None
            check_request = None
            first_mode = resolution.mode
            if (
                workflow
                and workflow.get("requires_check")
                and first_mode != "check"
            ):
                check_request = self._check_request_from_locked_choice(
                    workflow
                )
                resolution = replace(
                    resolution,
                    mode="check",
                    narrative="",
                    check=check_request,
                )
                first_mode = "check"
            if (
                workflow
                and not workflow.get("requires_check")
                and first_mode == "check"
            ):
                raise TavernEngineError(
                    "该选项未提前标记检定与风险，但模型临时申请投骰；"
                    "为避免隐藏加码，本轮没有提交"
                )
            if resolution.mode == "check":
                if resolution.check is None:
                    raise TavernEngineError("模型检定结构缺失")
                check_request = resolution.check
                selected_choice = (
                    dict(workflow.get("selected_choice") or {})
                    if workflow
                    and isinstance(workflow.get("selected_choice"), Mapping)
                    else {}
                )
                locked_advantages = tuple(
                    selected_choice.get("advantage_sources") or ()
                )
                locked_disadvantages = tuple(
                    selected_choice.get("disadvantage_sources") or ()
                )
                effective_stat = str(
                    selected_choice.get("check_stat")
                    or check_request.stat
                    or "通用"
                )
                authoritative = await self.database.authoritative_modifier(
                    session_id,
                    sender_id,
                    effective_stat,
                )
                check_context = await self.database.check_context(
                    session_id,
                    sender_id,
                    str(authoritative["stat"]),
                    proposed_advantages=check_request.advantage_sources,
                    proposed_disadvantages=check_request.disadvantage_sources,
                    locked_advantages=locked_advantages,
                    locked_disadvantages=locked_disadvantages,
                )
                inspiration_mode = str(
                    workflow.get("inspiration_mode") if workflow else ""
                ).lower()
                if inspiration_mode:
                    inspiration = await self.database.inspiration_status(
                        session_id,
                        sender_id,
                    )
                    if inspiration["balance"] < 1:
                        raise TavernEngineError("灵感点不足，本轮没有投骰")
                check_type = str(
                    selected_choice.get("check_type")
                    or check_request.check_type
                    or "standard"
                )
                if (
                    inspiration_mode
                    and check_type in {"group", "resistance"}
                ):
                    raise TavernEngineError(
                        "集体检定与独立抵抗不能由一名玩家替全队消耗灵感"
                    )
                dice_visibility = str(
                    (rule_state.get("dice_rules") or {}).get(
                        "visibility",
                        "public",
                    )
                ).lower()
                risk = str(
                    selected_choice.get("risk")
                    or check_request.risk
                    or "controlled"
                )
                known_consequences = str(
                    selected_choice.get("known_consequences")
                    or check_request.known_consequences
                    or ""
                )
                if risk == "lethal" and not known_consequences:
                    raise TavernEngineError(
                        "致命风险没有提前明示已知后果，本轮没有投骰"
                    )
                check_request = replace(
                    check_request,
                    stat=str(authoritative["stat"]),
                    modifier=int(authoritative["modifier"]),
                    difficulty=int(
                        selected_choice.get("difficulty")
                        or check_request.difficulty
                    ),
                    risk=risk,
                    check_type=check_type,
                    advantage_sources=tuple(check_context["advantages"]),
                    disadvantage_sources=tuple(
                        check_context["disadvantages"]
                    ),
                    known_consequences=known_consequences,
                    visibility=(
                        dice_visibility
                        if dice_visibility
                        in {"public", "immersive", "hidden"}
                        else "public"
                    ),
                    inspiration_mode=inspiration_mode,
                    opponent_modifier=0,
                )
                if workflow is not None:
                    workflow = {
                        **dict(workflow),
                        "assist_token_id": check_context.get(
                            "assist_token_id",
                            "",
                        ),
                    }
                operation_id = (
                    "dice:"
                    + session_id
                    + ":"
                    + str(
                        workflow.get("choice_set_id")
                        if workflow
                        else session["revision"]
                    )
                )
                receipt = await self.database.get_operation_receipt(
                    operation_id
                )
                if receipt:
                    locked_request = self._check_request_from_payload(
                        receipt["request"]
                    )
                    if (
                        locked_request.inspiration_mode
                        != check_request.inspiration_mode
                    ):
                        raise TavernEngineError(
                            "本次检定的骰池已经锁定，不能在重试时更换灵感用法"
                        )
                    check_request = locked_request
                    dice = self._dice_result_from_payload(receipt["result"])
                else:
                    if check_type in {"group", "resistance"}:
                        requested_ids = set(check_request.participant_ids)
                        actors: list[dict[str, Any]] = []
                        for member in await self.database.list_roster(
                            session_id
                        ):
                            if (
                                member.get("participation_status") != "active"
                                or member.get("card_status") != "approved"
                            ):
                                continue
                            if requested_ids and not (
                                {
                                    str(member.get("id") or ""),
                                    str(member.get("group_user_id") or ""),
                                }
                                & requested_ids
                            ):
                                continue
                            member_user_id = str(
                                member.get("group_user_id") or ""
                            )
                            member_modifier = (
                                await self.database.authoritative_modifier(
                                    session_id,
                                    member_user_id,
                                    check_request.stat,
                                )
                            )
                            member_context = (
                                await self.database.check_context(
                                    session_id,
                                    member_user_id,
                                    str(member_modifier["stat"]),
                                    proposed_advantages=(
                                        check_request.advantage_sources
                                    ),
                                    proposed_disadvantages=(
                                        check_request.disadvantage_sources
                                    ),
                                    locked_advantages=locked_advantages,
                                    locked_disadvantages=locked_disadvantages,
                                )
                            )
                            actors.append(
                                {
                                    "actor_id": member["id"],
                                    "name": (
                                        member.get("character_name")
                                        or member.get("display_name")
                                        or member_user_id
                                    ),
                                    "modifier": member_modifier["modifier"],
                                    "advantage_sources": member_context[
                                        "advantages"
                                    ],
                                    "disadvantage_sources": member_context[
                                        "disadvantages"
                                    ],
                                }
                            )
                        if not actors:
                            raise TavernEngineError(
                                "集体检定没有有效参与角色"
                            )
                        dice = roll_group_check(check_request, actors)
                    elif check_type == "opposed":
                        dice = roll_opposed_check(check_request)
                    else:
                        dice = roll_check(check_request)
                    receipt = await self.database.lock_check_result(
                        operation_id,
                        session_id,
                        asdict(check_request),
                        asdict(dice),
                    )
                    check_request = self._check_request_from_payload(
                        receipt["request"]
                    )
                    dice = self._dice_result_from_payload(
                        receipt["result"]
                    )
                check_prompt = checked_resolution_prompt(
                    session=session,
                    player=player,
                    player_input=player_input,
                    events=events,
                    memories=memories,
                    check=asdict(check_request),
                    dice=asdict(dice),
                )
                second_stage_providers = self._provider_order(
                    used_provider_id,
                    tuple(provider_ids),
                )
                resolution, used_provider_id = (
                    await self._generate_resolution(
                        provider_ids=second_stage_providers,
                        system=system,
                        prompt=check_prompt,
                        config=config,
                    )
                )
                if resolution.mode != "resolve":
                    raise TavernEngineError("模型未完成检定后的最终裁定")

            if workflow:
                if workflow.get("requires_check") and first_mode != "check":
                    raise TavernEngineError(
                        "该选项标记为必须检定，但模型未申请检定；"
                        "为避免越权结果，本轮没有提交"
                    )
                if (
                    workflow.get("collective")
                    and not resolution.group_decision
                ):
                    raise TavernEngineError(
                        "该选项影响全队，但模型没有生成集体表决；"
                        "为避免单人越权，本轮没有提交"
                    )
                if (
                    not resolution.next_choices
                    and not resolution.group_decision
                ):
                    raise TavernEngineError(
                        "模型未生成下一位玩家的 A/B/C/D 选项；"
                        "本轮没有提交"
                    )

            new_state = (
                dict(session.get("world_state") or {})
                if resolution.group_decision
                else apply_state_patch(
                    session.get("world_state"),
                    resolution.state_patch,
                )
            )
            normalized_memories = []
            for memory in resolution.memories:
                entry = dict(memory)
                if entry["scope"] == "player" and not entry["scope_id"]:
                    entry["scope_id"] = player["id"]
                normalized_memories.append(entry)

            narrative = resolution.narrative.strip()
            if len(narrative) > config.max_output_chars:
                narrative = narrative[: config.max_output_chars].rstrip() + "…"

            check_payload = asdict(dice) if dice else None
            if dice and check_request:
                check_payload = {
                    **check_payload,
                    "check_id": (
                        "check:"
                        + str(
                            workflow.get("choice_set_id")
                            if workflow
                            else session["revision"]
                        )
                    ),
                    "stat": check_request.stat,
                    "reason": check_request.reason,
                    "known_consequences": (
                        check_request.known_consequences
                    ),
                }
            commit_workflow = (
                {
                    **dict(workflow),
                    "next_choices": [
                        dict(item) for item in resolution.next_choices
                    ],
                    "group_decision": resolution.group_decision,
                    "return_progress": resolution.return_progress,
                    "npc_ops": [
                        dict(item) for item in resolution.npc_ops
                    ],
                    "clock_ops": [
                        dict(item) for item in resolution.clock_ops
                    ],
                    "ledger_ops": [
                        dict(item) for item in resolution.ledger_ops
                    ],
                    "status_ops": [
                        dict(item) for item in resolution.status_ops
                    ],
                    "assist_ops": [
                        dict(item) for item in resolution.assist_ops
                    ],
                }
                if workflow
                else None
            )
            try:
                updated_session = await self.database.commit_turn(
                    session_id=session_id,
                    expected_revision=session["revision"],
                    player_id=player["id"],
                    player_user_id=sender_id,
                    player_name=(
                        player["character_name"] or player["display_name"]
                    ),
                    player_input=player_input,
                    narrative=narrative,
                    world_state=new_state,
                    memories=normalized_memories,
                    check_payload=check_payload,
                    model_payload=resolution.raw,
                    director_note=resolution.director_note,
                    auto_snapshot_interval=config.auto_snapshot_interval,
                    store_model_payload=config.store_model_payloads,
                    workflow=commit_workflow,
                )
            except DatabaseConflictError as exc:
                raise TavernBusyError(
                    "本轮状态刚被其他操作更新，请重新提交行动"
                ) from exc

            await self.broker.publish(
                {
                    "type": "turn",
                    "session_id": session_id,
                    "group_id": updated_session["group_id"],
                    "turn_no": updated_session["turn_no"],
                    "actor": sender_name,
                    "checked": bool(dice),
                }
            )
            next_turn = await self.database.get_turn_status(session_id)
            output = narrative
            if dice:
                stat = (
                    check_payload.get("stat", "通用")
                    if check_payload
                    else "通用"
                )
                dice_text = self._format_dice_result(dice, stat)
                output = (
                    f"{dice_text}\n\n{narrative}"
                    if dice_text
                    else narrative
                )
            current_name = (
                next_turn["current_name"]
                or next_turn["current_user_id"]
                or "等待玩家加入"
            )
            workflow_result = (
                updated_session.get("workflow", {})
                if commit_workflow
                else {}
            )
            vote_pending = bool(workflow_result.get("vote_id"))
            if vote_pending:
                turn_footer = (
                    f"【回合秩序】{current_name} 的行动权已挂起 · "
                    "集体投票不消耗本次机会"
                )
            elif len(next_turn["order"]) > 1:
                if next_turn["round_no"] > acting_round:
                    turn_footer = (
                        f"【回合秩序】第 {acting_round} 轮结束 · "
                        f"第 {next_turn['round_no']} 轮：{current_name}"
                    )
                else:
                    turn_footer = (
                        f"【回合秩序】第 {acting_round} 轮 · "
                        f"下一位：{current_name}"
                    )
            else:
                turn_footer = (
                    f"【回合秩序】第 {next_turn['round_no']} 轮 · "
                    f"当前：{current_name}"
                )
            output = f"{output}\n\n{turn_footer}"
            if commit_workflow:
                world_event = workflow_result.get("world_event")
                if world_event:
                    output += (
                        "\n\n【世界脉冲】"
                        f"{world_event.get('title') or '局势变化'}\n"
                        f"{world_event.get('description')}"
                    )
                vote_id = workflow_result.get("vote_id")
                if vote_id:
                    vote = await self.database.active_vote(session_id)
                    if vote:
                        vote_lines = [
                            "【集体决策】",
                            vote["question"],
                            *[
                                f"{item.get('key')}. {item.get('text')}"
                                for item in vote["options"]
                            ],
                            "",
                            "发送：/酒馆 投票 A",
                            "投票期间不消耗当前玩家的行动机会。",
                        ]
                        output += "\n\n" + "\n".join(vote_lines)
                else:
                    next_choice = await self.database.active_choice_set(
                        session_id
                    )
                    if next_choice and next_choice.get("participant"):
                        next_actor = next_choice["participant"]
                        output += "\n\n" + format_choices(
                            next_actor["character_name"]
                            or next_actor["display_name"],
                            next_choice["choices"],
                            rerolls_left=(
                                1 - int(next_choice["reroll_count"])
                            ),
                        )
            return EngineReply(
                text=output,
                session=updated_session,
                dice=dice,
                turn=next_turn,
            )

    async def _generate_choices(
        self,
        *,
        provider_ids: Sequence[str],
        world: Mapping[str, Any],
        session: Mapping[str, Any],
        participant: Mapping[str, Any],
        events: Sequence[Mapping[str, Any]],
        config: TavernConfig,
        avoid: Sequence[Mapping[str, Any]] = (),
    ) -> list[dict[str, Any]]:
        prompt = (
            "只生成当前角色在当前场景可选的四个行动意图。"
            "返回单个 JSON 对象，字段名 choices。"
            "必须恰好包含 A、B、C、D，至少一个 safe 风险；"
            "不得预设成功，不得添加角色没有的能力、物品或知识；"
            "风险使用 safe/controlled/dangerous/desperate/lethal；"
            "需要检定时同时给出 check_type、check_stat、difficulty、"
            "known_consequences 以及已经成立的优劣势来源；"
            "致命风险必须明确可能后果，同一原因不能同时提高 DC 和造成劣势；"
            "影响全队的转场、主线分支、共有资源或不可逆决定，"
            "只能标记 collective=true，不能替队伍决定。\n\n"
            f"<world_rules>{world.get('rules', {})}</world_rules>\n"
            f"<runtime_state>{session.get('world_state', {})}</runtime_state>\n"
            f"<acting_character>{dict(participant)}</acting_character>\n"
            f"<recent_history>{list(events)[-12:]}</recent_history>\n"
            f"<avoid_repeating>{list(avoid)}</avoid_repeating>\n"
            "格式示例："
            '{"choices":[{"key":"A","text":"...","risk":"safe",'
            '"requires_check":false,"collective":false,"difficulty":12},'
            '{"key":"B","text":"...","risk":"controlled",'
            '"requires_check":true,"collective":false,"check_type":"standard",'
            '"check_stat":"敏捷","difficulty":12,"known_consequences":"..."},'
            '{"key":"C","text":"...","risk":"controlled",'
            '"requires_check":false,"collective":false},'
            '{"key":"D","text":"...","risk":"dangerous",'
            '"requires_check":true,"collective":true}]}'
        )
        failures: list[str] = []
        for provider_id in provider_ids:
            try:
                response = await asyncio.wait_for(
                    self.context.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=prompt,
                        system_prompt=system_prompt(world),
                        temperature=config.temperature,
                        max_tokens=min(config.max_tokens, 1200),
                    ),
                    timeout=config.request_timeout_seconds,
                )
                payload = extract_json_object(
                    str(getattr(response, "completion_text", "") or "")
                )
                choices = normalize_choices(
                    payload.get("choices", payload.get("next_choices"))
                )
                await self.database.record_provider_result(
                    provider_id,
                    success=True,
                )
                return choices
            except TimeoutError:
                failures.append(f"{provider_id}：请求超时")
                await self.database.record_provider_result(
                    provider_id,
                    success=False,
                    reason="选项生成请求超时",
                )
            except Exception as exc:
                failures.append(f"{provider_id}：{exc}")
                await self.database.record_provider_result(
                    provider_id,
                    success=False,
                    reason=f"选项生成失败：{type(exc).__name__}: {exc}",
                )
        raise TavernEngineError(
            "未能生成一组合法的新选项："
            + ("；".join(failures) or "没有可用模型")
        )

    async def _generate_resolution(
        self,
        *,
        provider_ids: list[str],
        system: str,
        prompt: str,
        config: TavernConfig,
    ) -> tuple[Resolution, str]:
        attempts = config.json_repair_attempts + 1
        original_prompt = prompt
        failures: list[str] = []
        for provider_id in provider_ids:
            current_prompt = prompt
            last_error = ""
            provider_failed = False
            for attempt in range(attempts):
                try:
                    response = await asyncio.wait_for(
                        self.context.llm_generate(
                            chat_provider_id=provider_id,
                            prompt=current_prompt,
                            system_prompt=system,
                            temperature=config.temperature,
                            max_tokens=config.max_tokens,
                        ),
                        timeout=config.request_timeout_seconds,
                    )
                except TimeoutError:
                    failures.append(f"{provider_id}：请求超时")
                    await self.database.record_provider_result(
                        provider_id,
                        success=False,
                        reason="叙事请求超时",
                    )
                    provider_failed = True
                    break
                except Exception as exc:
                    failures.append(
                        f"{provider_id}：{type(exc).__name__}"
                    )
                    await self.database.record_provider_result(
                        provider_id,
                        success=False,
                        reason=f"叙事调用失败：{type(exc).__name__}",
                    )
                    provider_failed = True
                    break

                raw = str(
                    getattr(response, "completion_text", "") or ""
                )
                try:
                    payload = extract_json_object(raw)
                    resolution = validate_resolution(payload)
                    await self.database.record_provider_result(
                        provider_id,
                        success=True,
                    )
                    return resolution, provider_id
                except (TypeError, ValueError) as exc:
                    last_error = str(exc)
                    if attempt + 1 >= attempts:
                        break
                    current_prompt = repair_prompt(
                        raw,
                        last_error,
                        original_prompt,
                    )
            if not provider_failed:
                await self.database.record_provider_result(
                    provider_id,
                    success=False,
                    reason=f"结构校验失败：{last_error or '未知错误'}",
                )
                failures.append(
                    f"{provider_id}：结构校验失败"
                    f"（{last_error or '未知错误'}）"
                )

        summary = "；".join(failures) or "没有可用模型"
        raise TavernEngineError(
            f"全部叙事模型均未完成本轮：{summary}"
        )
