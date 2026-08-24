from .shared import *
from .errors import *

class EngineCoreMixin:
    def __init__(
        self,
        *,
        context: Any,
        database: TavernDatabase,
        config_provider: Callable[[], TavernConfig],
        broker: EventBroker,
        extensions: ExtensionRegistry | None = None,
    ) -> None:
        self.context = context
        self.database = database
        self.config_provider = config_provider
        self.broker = broker
        self.extensions = extensions or ExtensionRegistry()
        if self.extensions.resolve("dice_system", "d20") is None:
            self.extensions.register_dice_system("d20", _builtin_d20_provider)
        self.rate_limiter = RateLimiter()
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    @staticmethod
    async def _emit_progress(
        callback: ProgressCallback | None,
        message: ProgressPayload,
    ) -> None:
        if callback is None:
            return
        result = callback(message)
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def _effective_generation_reminder(
        instance: Mapping[str, Any],
        config: TavernConfig,
    ) -> GenerationReminderConfig:
        time_rules = instance.get("time_rules")
        time_rules = time_rules if isinstance(time_rules, Mapping) else {}
        snapshot = time_rules.get("story_generation_reminder")
        snapshot = snapshot if isinstance(snapshot, Mapping) else {}
        source = str(snapshot.get("source") or "implicit_default")
        if source == "implicit_default":
            return GenerationReminderConfig(
                enabled=True,
                interval_seconds=60,
                source="implicit_default",
                revision=int(snapshot.get("revision") or 0),
                source_revision=int(snapshot.get("source_revision") or 0),
            )
        return GenerationReminderConfig.from_mapping(
            snapshot,
            source=source,
            revision=snapshot.get("revision", 0),
            fail_safe=True,
        )

    @staticmethod
    def _narrative_speaker_contract(
        session_id: str,
        world: Mapping[str, Any],
        roster: Sequence[Mapping[str, Any]],
    ) -> tuple[dict[str, str], tuple[str, ...], tuple[str, ...], list[dict[str, Any]]]:
        speakers: dict[str, str] = {}
        player_refs: list[str] = []
        player_labels: list[str] = []
        prompt_rows: list[dict[str, Any]] = []

        def register(item: Mapping[str, Any], *, player: bool) -> None:
            stable = str(
                item.get("actor_id")
                or item.get("id")
                or item.get("stable_key")
                or item.get("slug")
                or item.get("group_user_id")
                or ""
            ).strip()
            label = clean_text(
                item.get("character_name")
                or item.get("name")
                or item.get("display_name"),
                max_chars=80,
            )
            if not stable or not label:
                return
            actor_ref = "actor_" + hashlib.sha256(
                f"{session_id}\0{stable}".encode("utf-8")
            ).hexdigest()[:20]
            if actor_ref in speakers:
                return
            speakers[actor_ref] = label
            prompt_rows.append(
                {
                    "actor_ref": actor_ref,
                    "label": label,
                    "player": player,
                }
            )
            if player:
                player_refs.append(actor_ref)
                player_labels.append(label)

        for item in roster:
            if not isinstance(item, Mapping):
                continue
            kind = str(item.get("actor_kind") or "human").lower()
            register(item, player=kind not in {"ai_companion", "npc"})
        for item in world.get("characters") or ():
            if isinstance(item, Mapping):
                register(item, player=False)
        return (
            speakers,
            tuple(player_refs),
            tuple(player_labels),
            prompt_rows,
        )

    async def _emit_operation_progress(
        self,
        operation_id: str,
        stage: str,
        callback: ProgressCallback | None,
        message: ProgressPayload | None,
        *,
        acknowledge: bool = False,
    ) -> None:
        receipt = await self.database.record_operation_progress(
            operation_id,
            stage,
        )
        emit = bool(receipt.get("emit"))
        if acknowledge:
            ack = await self.database.claim_generation_ack(operation_id)
            emit = bool(ack.get("emit"))
        if callback is not None and message is not None and emit:
            await self._emit_progress(callback, message)

    async def _raise_if_operation_cancelled(self, operation_id: str) -> None:
        state = await self.database.get_operation_state(operation_id)
        status = str((state or {}).get("status") or "")
        if status == "cancel_requested":
            await self.database.mark_operation_cancelled(operation_id)
            status = "cancelled"
        if status == "cancelled":
            raise TavernOperationCancelled(
                "生成故事已取消。\n"
                "原因：有权限的参与者取消了尚未提交的本轮操作。\n"
                "自动处理：系统已丢弃迟到结果，世界、投票和检定凭证没有被重复提交。\n"
                "下一步：重新发送行动，或由主持人发送 /团 恢复。"
            )
        if status == "needs_recovery":
            raise TavernBusyError(
                "本轮提交状态需要恢复核对。系统已锁住新回合，"
                "请在控制台完成诊断后发送 /团 重试本轮。"
            )

    async def _roll_with_registered_system(
        self,
        world: Mapping[str, Any],
        check: CheckRequest,
        *,
        actors: list[Mapping[str, Any]] | None = None,
    ) -> DiceResult:
        contract = world_contract(world)
        system_name = str(
            contract["resolution"].get("dice_system") or ""
        ).strip().lower()
        provider = self.extensions.resolve("dice_system", system_name)
        if provider is None:
            raise TavernEngineError(
                f"世界要求骰制“{system_name}”，但运行时没有注册该骰制"
            )
        result = provider(
            check=check,
            check_type=check.check_type,
            actors=list(actors or []),
            outcome_policy=contract["resolution"].get("outcome_policy") or {},
        )
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, DiceResult):
            raise TavernEngineError(
                f"骰制“{system_name}”没有返回合法 DiceResult"
            )
        return result

    def validate_world_runtime(self, world: Mapping[str, Any]) -> None:
        contract = world_contract(world)
        if contract["resolution"]["mode"] not in {"dice_only", "attribute"}:
            return
        system_name = str(
            contract["resolution"].get("dice_system") or ""
        ).strip().lower()
        if self.extensions.resolve("dice_system", system_name) is None:
            raise TavernEngineError(
                f"世界要求骰制“{system_name}”，但运行时没有注册该骰制"
            )

    async def _session_lock(self, session_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[session_id] = lock
            return lock

    async def release_session_lock(self, session_id: str) -> None:
        """0.11.1：副本关闭/完结/删除后回收会话锁，避免 _locks 无限增长。

        1.0.0-A3：同时清理该会话的速率限制记录（clear_session），
        避免 RateLimiter 的 (session_id, sender_id) 条目无限累积。
        """
        async with self._locks_guard:
            lock = self._locks.get(session_id)
            if lock is not None and not lock.locked():
                self._locks.pop(session_id, None)
        self.rate_limiter.clear_session(session_id)

    @staticmethod
    def _first_turn_opening_context(
        world: Mapping[str, Any],
        session: Mapping[str, Any],
        roster: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any] | None:
        """D1 首轮上下文：显式消费 scene_graph 并投影世界声明的开场场景。

        仅在副本首轮（turn_no == 0）注入，让叙事模型的首轮裁定直接
        基于开场战役的入口 NPC、职业行动入口、首轮冲突、开场节奏与
        推荐转场，而不是把开场语义留给模型自行猜测。
        """
        if int(session.get("turn_no") or 0) != 0:
            return None
        rules = world.get("rules")
        rules = rules if isinstance(rules, Mapping) else {}
        scene_graph = rules.get("scene_graph")
        if not isinstance(scene_graph, Mapping) or not scene_graph:
            return None
        projection = project_opening_scene(
            world,
            session.get("world_state") or {},
            squad=roster or (),
        )
        if not projection.get("declared"):
            return None
        return projection

    @staticmethod
    def _format_stall_guidance(plan: Mapping[str, Any]) -> str:
        """把停滞干预计划渲染为玩家/主持人可直接执行的引导文本。"""
        if not isinstance(plan, Mapping) or not plan.get("stalled"):
            return ""
        lines = [
            "⏳ 【节奏提示】",
            str(
                plan.get("summary")
                or "当前场景已连续多轮没有新的状态变化，请选择一项推进。"
            ),
        ]
        for order, item in enumerate(plan.get("actions") or [], 1):
            if not isinstance(item, Mapping):
                continue
            label = str(item.get("label") or "")
            if not label:
                continue
            cost = item.get("cost")
            suffix = ""
            if isinstance(cost, Mapping):
                cost_label = str(cost.get("label") or "").strip()
                description = str(cost.get("description") or "").strip()
                if cost_label:
                    suffix = f"（代价：{cost_label}）"
                elif description:
                    suffix = f"（代价：{description}）"
            lines.append(f"{order}. {label}{suffix}")
        lines.append("主持人可选择暂停，或引导队伍按上述转场继续推进。")
        return "\n".join(lines)

    @staticmethod
    def _usage_value(source: Any, *names: str) -> int:
        for name in names:
            if isinstance(source, Mapping):
                value = source.get(name)
            else:
                value = getattr(source, name, None)
            try:
                if value is not None:
                    return max(0, int(value))
            except (TypeError, ValueError, OverflowError):
                continue
        return 0

    @classmethod
    def _response_usage(
        cls,
        response: Any,
        *,
        prompt: str,
        system: str,
    ) -> tuple[int, int, int, str]:
        usage = (
            getattr(response, "usage", None)
            or getattr(response, "token_usage", None)
            or getattr(response, "usage_metadata", None)
        )
        input_tokens = cls._usage_value(
            usage,
            "input_tokens",
            "prompt_tokens",
            "input",
            "input_other",
        )
        cached = cls._usage_value(
            usage,
            "cached_input_tokens",
            "input_cached_tokens",
            "cache_read_input_tokens",
            "input_cached",
        )
        output_tokens = cls._usage_value(
            usage,
            "output_tokens",
            "completion_tokens",
            "output",
        )
        if input_tokens or output_tokens:
            return input_tokens, cached, output_tokens, "provider"
        completion = str(
            getattr(response, "completion_text", "") or ""
        )
        estimated_input = max(
            1,
            (len(str(prompt or "")) + len(str(system or "")) + 1) // 2,
        )
        estimated_output = max(1, (len(completion) + 1) // 2)
        return estimated_input, 0, estimated_output, "estimated"

    async def _llm_generate_metered(
        self,
        *,
        session_id: str,
        request_type: str,
        provider_id: str,
        prompt: str,
        system_prompt_value: str = "",
        max_tokens: int,
        **kwargs: Any,
    ) -> Any:
        from ..context_budget import enforce_hard_input_budget

        enforce_hard_input_budget(
            prompt,
            system_prompt_value,
            reserved_output_tokens=max_tokens,
        )
        expected_input = max(
            1,
            len(str(prompt or ""))
            + len(str(system_prompt_value or "")),
        )
        reservation = await self.database.reserve_token_usage(
            session_id,
            request_type,
            provider_id,
            expected_input + max(1, int(max_tokens)),
        )
        try:
            response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=system_prompt_value or None,
                max_tokens=max_tokens,
                **kwargs,
            )
        except Exception:
            await self.database.fail_token_usage(reservation["id"])
            raise
        input_tokens, cached, output_tokens, source = self._response_usage(
            response,
            prompt=prompt,
            system=system_prompt_value,
        )
        await self.database.settle_token_usage(
            reservation["id"],
            input_tokens=input_tokens,
            cached_input_tokens=cached,
            output_tokens=output_tokens,
            usage_source=source,
        )
        return response

    @staticmethod
    def _visible_length(value: str) -> int:
        return len(re.sub(r"\s+", "", str(value or "")))

    @classmethod
    def _validate_mobile_resolution(
        cls,
        resolution: Resolution,
        *,
        expected_actor: Mapping[str, Any] | None,
        roster: Sequence[Mapping[str, Any]],
        narrative_mode: object = None,
    ) -> Resolution:
        if resolution.mode == "resolve":
            length = cls._visible_length(resolution.narrative)
            minimum, maximum = narrative_quality_policy(
                narrative_mode
            ).bounds("turn")
            if length < minimum or length > maximum:
                raise ValueError(
                    f"故事正文必须为 {minimum}—{maximum} 字，"
                    f"当前为 {length} 字"
                )
        if not resolution.next_choices:
            return resolution
        normalized = cls._validate_choices_for_actor(
            resolution.next_choices,
            expected_actor=expected_actor,
            roster=roster,
        )
        return replace(resolution, next_choices=tuple(normalized))

    @classmethod
    def _validate_choices_for_actor(
        cls,
        choices: Sequence[Mapping[str, Any]],
        *,
        expected_actor: Mapping[str, Any] | None,
        roster: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        expected_id = str(
            (expected_actor or {}).get("id")
            or (expected_actor or {}).get("participant_id")
            or ""
        )
        expected_name = str(
            (expected_actor or {}).get("character_name")
            or (expected_actor or {}).get("display_name")
            or ""
        )
        if not expected_id:
            return [dict(item) for item in choices]
        other_names = {
            str(
                item.get("character_name")
                or item.get("display_name")
                or ""
            ).strip()
            for item in roster
            if isinstance(item, Mapping)
            and str(item.get("id") or "") != expected_id
        }
        other_names.discard("")
        control_words = ("让", "命令", "迫使", "替", "控制", "要求")
        normalized: list[dict[str, Any]] = []
        for item in choices:
            option = dict(item)
            actor_id = str(option.get("actor_id") or "").strip()
            if actor_id and actor_id != expected_id:
                raise ValueError(
                    "行动选项 actor_id 与下一位行动角色不一致"
                )
            option["actor_id"] = expected_id
            text = str(option.get("text") or "")
            if cls._visible_length(text) > 50:
                raise ValueError("行动选项不得超过 50 字")
            for name in other_names:
                if any(
                    marker + name in text
                    for marker in control_words
                ) or text.startswith(name + "决定"):
                    raise ValueError(
                        f"选项越权操控了其他玩家角色 {name}"
                    )
            if expected_name and text.startswith(expected_name + "让"):
                # “自己让别人执行”仍然是替他人决定行动。
                raise ValueError("选项不能借当前角色回合操控他人")
            normalized.append(option)
        return normalized

    @staticmethod
    def _next_actor(
        turn: Mapping[str, Any],
        roster: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        raw_order = turn.get("order")
        order = [
            str(item.get("user_id") or "")
            for item in raw_order
            if isinstance(item, Mapping) and item.get("user_id")
        ] if isinstance(raw_order, list) else []
        current = str(turn.get("current_user_id") or "")
        if not order:
            return {}
        if current in order and len(order) > 1:
            next_user = order[(order.index(current) + 1) % len(order)]
        else:
            next_user = current or order[0]
        item = next(
            (
                dict(entry)
                for entry in roster
                if str(
                    entry.get("group_user_id")
                    or entry.get("actor_ref")
                    or ""
                )
                == next_user
            ),
            {},
        )
        if not item:
            return {"group_user_id": next_user}
        return {
            "participant_id": item.get("id"),
            "id": item.get("id"),
            "actor_id": item.get("actor_id"),
            "actor_ref": item.get("actor_ref"),
            "actor_kind": item.get("actor_kind", "human"),
            "group_user_id": item.get("group_user_id"),
            "character_name": item.get("character_name"),
            "character_code": item.get("character_code"),
            "display_name": item.get("display_name"),
            "profile": item.get("card_profile", {}),
            "stats": item.get("card_stats", {}),
            "runtime_state": item.get("runtime_state", {}),
        }

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
                    f"- 「{str(item.get('name') or '').strip()}」："
                    f"{'、'.join(str(roll) for roll in item.get('rolls') or [])} "
                    f"{int(item.get('modifier') or 0):+d} → "
                    f"{item.get('total')} · "
                    f"{labels.get(str(item.get('outcome')), item.get('outcome'))}"
                )
                for item in dice.members
                if str(item.get("name") or "").strip()
            ]
            missing_names = sum(
                1
                for item in dice.members
                if not str(item.get("name") or "").strip()
            )
            if missing_names:
                member_lines.append(
                    f"- 参与者资料读取失败：{missing_names} 名成员缺少可公开显示的名称，"
                    "系统未显示其检定明细。"
                )
            header = (
                f"🎲【{stat}·{mode_labels.get(dice.dice_mode, dice.dice_mode)}"
                f"检定】{dice.total}/{dice.difficulty} 人达标 · "
                f"{result_label}"
            )
            return "\n".join([header, *member_lines])
        rolls = list(dice.rolls)
        pool = (
            f"掷出 {'、'.join(str(roll) for roll in rolls)} → 取 {dice.kept}"
            if len(rolls) > 1
            else f"掷出 {dice.kept}"
        )
        modifier = f"{dice.modifier:+d}"
        header = (
            f"🎲【{stat}检定】"
            f"［{mode_labels.get(dice.dice_mode, dice.dice_mode)}］"
            f"{pool} {modifier} → {dice.total}"
        )
        if dice.visibility == "public":
            header += f" / 难度 {dice.difficulty}"
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
                    "灵感重投：原结果 "
                    + "、".join(str(roll) for roll in dice.original_rolls)
                )
            if dice.check_type == "opposed" and dice.members:
                defender = dice.members[0]
                source_lines.append(
                    "对抗方："
                    f"{defender.get('name') or '防守方'} "
                    f"{'、'.join(str(roll) for roll in defender.get('rolls') or [])} "
                    f"{int(defender.get('modifier') or 0):+d} → "
                    f"{defender.get('total')}"
                )
            if source_lines:
                header += "\n" + "\n".join(source_lines)
        return header

    async def _publish_locked_check_progress(
        self,
        callback: ProgressCallback | None,
        dice: DiceResult,
        stat: str,
    ) -> None:
        dice_text = self._format_dice_result(dice, stat)
        if dice_text:
            await self._emit_progress(callback, dice_text)
        await self._emit_progress(
            callback,
            PlayerMessage.dynamic(
                title="故事生成",
                summary="已收到你的选择，正在结算本轮行动。",
                sections=(
                    "自动处理：检定结果已经锁定，世界状态尚未改变。",
                ),
                actions=("/团 取消",),
                source="story_generation_progress",
            ),
        )

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
            healthy = await self.database.filter_healthy_providers(providers)
            if healthy:
                return healthy
            raise TavernStoryGenerationError(
                "已配置的叙事模型当前均处于断路或不可用状态",
                failure_kinds=("unavailable",),
            )
        if current_error:
            raise TavernStoryGenerationError(
                "无法取得当前群会话模型，且没有配置备用模型",
                failure_kinds=("unavailable",),
            ) from current_error
        raise TavernStoryGenerationError(
            "没有可用的叙事模型",
            failure_kinds=("unavailable",),
        )

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
        session_id: str,
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
                self._llm_generate_metered(
                    session_id=session_id,
                    request_type="image_caption",
                    provider_id=config.image_caption_provider_id,
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
