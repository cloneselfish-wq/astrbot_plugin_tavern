from .shared import *
from .errors import *


class GenerationMixin:
    async def _build_turn_context(
        self,
        *,
        next_actor: Mapping[str, Any],
        roster: Sequence[Mapping[str, Any]],
        world: Mapping[str, Any],
        session: Mapping[str, Any],
        session_id: str,
    ) -> str:
        """1.0.0-A7：回合信息小节（种族/阵营/血脉 · 状态 · 技能 · 防具 · 武器 · 背包 · 资金）。

        数据均来自权威来源：角色卡档案（profile）、运行时状态（runtime_state）、
        item_instances、economy_summary；字段缺失/为空时省略对应行。
        """
        member = next(
            (
                item
                for item in roster
                if str(item.get("id") or "") == str(next_actor.get("id") or "")
                or str(item.get("group_user_id") or "") == str(
                    next_actor.get("group_user_id") or next_actor.get("user_id") or ""
                )
                or str(item.get("character_name") or "") == str(next_actor.get("character_name") or "")
            ),
            next_actor,
        )
        profile = member.get("card_profile")
        profile = profile if isinstance(profile, Mapping) else member.get("profile") or {}
        runtime = member.get("runtime_state")
        runtime = runtime if isinstance(runtime, Mapping) else {}

        lines: list[str] = []
        actor_view = project_actor_view(
            world,
            profile,
            viewer_role="character",
        )
        semantic_items = {
            str(item.get("role") or ""): item
            for section in actor_view.get("sections") or []
            if isinstance(section, Mapping)
            for item in section.get("items") or []
            if isinstance(item, Mapping) and item.get("role")
        }
        icon_defaults = {
            "actor.identity.species": "👥",
            "actor.identity.faction": "🏰",
            "actor.identity.bloodline": "🩸",
            "actor.capability.list": "⚡",
            "actor.equipment.armor": "🦺",
            "actor.equipment.weapon": "🔪",
        }
        identity_parts: list[str] = []
        for semantic_role in (
            "actor.identity.species",
            "actor.identity.faction",
            "actor.identity.bloodline",
        ):
            item = semantic_items.get(semantic_role, {})
            value = str(item.get("display_value") or "").strip()
            if value:
                identity_parts.append(
                    f"{icon_defaults[semantic_role]} "
                    f"{item.get('label') or semantic_role}：{value}"
                )
        if identity_parts:
            lines.append(" · ".join(identity_parts))
        # 状态
        statuses = runtime.get("statuses") if isinstance(runtime.get("statuses"), list) else []
        if statuses:
            status_names = [
                str(item.get("name") or "") for item in statuses if isinstance(item, dict) and item.get("name")
            ]
            lines.append("❤️ 状态：" + " · ".join(status_names[:6]))
        for semantic_role in (
            "actor.capability.list",
            "actor.equipment.armor",
            "actor.equipment.weapon",
        ):
            item = semantic_items.get(semantic_role, {})
            value = str(item.get("display_value") or "").strip()
            if value:
                lines.append(
                    f"{icon_defaults[semantic_role]} "
                    f"{item.get('label') or semantic_role}：{value}"
                )
        # 背包：item_instances 是唯一权威。
        keys = self._actor_owner_keys(member)
        owned: list[str] = []
        seen_instances: set[str] = set()
        from ..item_catalog import item_label

        for owner_ref in keys:
            try:
                instances = await self.database.list_item_instances(
                    session_id,
                    owner_ref,
                )
            except Exception:
                continue
            for instance in instances:
                if not isinstance(instance, Mapping):
                    continue
                identity = str(instance.get("id") or "")
                if identity and identity in seen_instances:
                    continue
                if identity:
                    seen_instances.add(identity)
                item_id = str(instance.get("item_id") or "")
                quantity = int(instance.get("quantity") or 0)
                if item_id and quantity > 0:
                    owned.append(
                        f"{item_label(world, item_id)} ×{quantity}"
                    )
        if owned:
            shown = " · ".join(owned[:8])
            if len(owned) > 8:
                shown += f" … 等 {len(owned)} 件"
            lines.append("🎒 背包：" + shown)
        # 资金
        try:
            economy = await self.database.economy_summary(session_id)
        except Exception:
            economy = {}
        currencies = {
            str(item.get("currency_id")): item
            for item in (economy.get("currencies") or []) if isinstance(item, Mapping)
        }
        wallets = economy.get("wallets") or []
        funds: list[str] = []
        for wallet in wallets:
            if not isinstance(wallet, Mapping):
                continue
            if str(wallet.get("owner_type") or "") != "character":
                continue
            if str(wallet.get("owner_ref") or "") not in keys:
                continue
            cid = str(wallet.get("currency_id") or "")
            meta = currencies.get(cid, {})
            label = str(meta.get("short_name") or meta.get("name") or cid)
            balance = wallet.get("balance")
            if isinstance(balance, (int, float)):
                funds.append(f"{label} {int(balance)}")
        if funds:
            lines.append("💰 资金：" + " · ".join(funds[:6]))
        return "\n".join(lines)

    def _normalize_state_patch_relationships(
        self,
        state_patch: Mapping[str, Any],
        roster: Any,
    ) -> dict[str, Any]:
        """A16：把 relationship_ops 的 source/target 规范化为稳定引用，
        避免模型输出裸 UUID 后关系键无法解析（配合统一实体解析器）。"""
        if not isinstance(state_patch, Mapping):
            return dict(state_patch or {})
        ops = state_patch.get("relationship_ops")
        if not isinstance(ops, list) or not ops:
            return dict(state_patch)
        labels = build_participant_labels(roster)
        normalized = normalize_relationship_ops(ops, labels)
        return {**dict(state_patch), "relationship_ops": normalized}

    async def _ensure_next_choices(
        self,
        *,
        resolution: Resolution,
        provider_ids: Sequence[str],
        world: Mapping[str, Any],
        session: Mapping[str, Any],
        participant: Mapping[str, Any],
        roster: Sequence[Mapping[str, Any]],
        events: Sequence[Mapping[str, Any]],
        candidate_state: Mapping[str, Any],
        config: TavernConfig,
        budget: TurnGenerationBudget | None = None,
        operation_id: str = "",
    ) -> Resolution:
        raw_choices = resolution.raw.get("next_choices")
        validation_error = str(
            resolution.raw.get("_next_choices_error") or ""
        )
        if resolution.next_choices:
            try:
                choices = self._validate_choices_for_actor(
                    resolution.next_choices,
                    expected_actor=participant,
                    roster=roster,
                )
                return replace(
                    resolution,
                    next_choices=tuple(choices),
                )
            except (TypeError, ValueError) as exc:
                # A16：actor_id 与下一位行动角色不一致时不再硬失败，
                # 进入专用修复/兜底路径（避免“正在生成”后误报身份错误）。
                validation_error = str(exc)
                logger.warning(
                    "321开团选项 actor_id 校验失败，进入修复：%s "
                    "expected=%s provided=%s",
                    exc,
                    str(
                        participant.get("id")
                        or participant.get("participant_id")
                        or ""
                    ),
                    [
                        str(item.get("actor_id") or "")
                        for item in resolution.next_choices
                    ],
                )

        if raw_choices is not None:
            try:
                choices = normalize_model_choices(raw_choices, world)
                choices = self._validate_choices_for_actor(
                    choices,
                    expected_actor=participant,
                    roster=roster,
                )
                return replace(
                    resolution,
                    next_choices=tuple(choices),
                )
            except (TypeError, ValueError) as exc:
                validation_error = str(exc)
        elif not validation_error:
            validation_error = "模型未提供 next_choices"

        avoid = (
            [
                dict(item)
                for item in raw_choices
                if isinstance(item, Mapping)
            ]
            if isinstance(raw_choices, Sequence)
            and not isinstance(raw_choices, (str, bytes))
            else []
        )
        choice_session = dict(session)
        choice_session["world_state"] = dict(candidate_state)
        recovery_method = "repaired"
        recovery_meta: dict[str, Any] = {}
        try:
            choices, recovery_meta = await self._generate_choices(
                provider_ids=provider_ids,
                world=world,
                session=choice_session,
                participant=participant,
                roster=roster,
                events=events,
                config=config,
                avoid=avoid,
                request_type="story_choices",
                validation_error=validation_error,
                story_context=resolution.narrative,
                budget=budget,
            )
        except TavernEngineError as exc:
            recovery_method = "fallback"
            logger.warning(
                "321开团选项专用修复失败，已使用安全兜底："
                "session=%s initial_error=%s repair_error=%s",
                session.get("id") or "",
                validation_error,
                exc,
            )
            choices = fallback_choices(candidate_state, world)
            expected_actor_id = str(
                participant.get("id")
                or participant.get("participant_id")
                or ""
            )
            for choice in choices:
                choice["actor_id"] = expected_actor_id
            choices = self._validate_choices_for_actor(
                choices,
                expected_actor=participant,
                roster=roster,
            )

        raw_payload = dict(resolution.raw)
        raw_payload["next_choices"] = [dict(item) for item in choices]
        trace_id = hashlib.sha256(
            str(operation_id or session.get("id") or "").encode("utf-8")
        ).hexdigest()[:8].upper()
        raw_payload["choice_recovery_receipt"] = {
            "schema": "tavern-choice-recovery-receipt/1.0.0-rc10",
            "status": recovery_method,
            "failure_kind": _choice_failure_kind(validation_error),
            "recovery_method": (
                "model_repair"
                if recovery_method == "repaired"
                else "local_fallback"
            ),
            "repair_count": 1 if recovery_method == "repaired" else 0,
            "fallback_version": (
                "choices-fallback/1.0.0-rc10"
                if recovery_method == "fallback"
                else ""
            ),
            "provider_class": str(
                recovery_meta.get("provider_class") or "none"
            ),
            "message": (
                "原选项未通过安全校验，系统已完成一次结构化修复。"
                if recovery_method == "repaired"
                else (
                    "选项生成与修复均未通过安全校验，系统已改用"
                    "包含安全行动和合法尝试的本地兜底。"
                )
            ),
            "trace_id": trace_id,
            "operation_id": str(operation_id),
            "idempotency_key": f"{operation_id}:choice-recovery",
            "resolution_summary": {
                **_choice_risk_summary(choices),
                "source": (
                    "repair"
                    if recovery_method == "repaired"
                    else "fallback"
                ),
                "validation_errors": [validation_error] if validation_error else [],
                "before": _choice_risk_summary(avoid),
                "after": _choice_risk_summary(choices),
            },
        }
        return replace(
            resolution,
            next_choices=tuple(choices),
            raw=raw_payload,
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
        roster: Sequence[Mapping[str, Any]] = (),
        request_type: str = "choice_reroll",
        validation_error: str = "",
        story_context: str = "",
        budget: TurnGenerationBudget | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        prompt = choice_generation_prompt(
            world=world,
            session=session,
            participant=participant,
            events=events,
            avoid=avoid,
            validation_error=validation_error,
            story_context=story_context,
        )
        choice_system = choice_system_prompt(world)
        failures: list[str] = []
        failure_kinds: list[str] = []
        budget = budget or self._new_generation_budget(config)
        attempts = min(1, max(0, int(config.json_repair_attempts))) + 1
        total_attempts = 0
        for provider_index, provider_id in enumerate(provider_ids):
            if provider_index > 0 and not budget.consume_fallback():
                break
            current_prompt = prompt
            last_error = ""
            timed_out = False
            for attempt in range(attempts):
                if attempt > 0 and not budget.consume_repair():
                    break
                if total_attempts >= _MAX_TOTAL_MODEL_ATTEMPTS:
                    failures.append(
                        f"{provider_id}：达到全局模型重试上限"
                    )
                    timed_out = True
                    failure_kinds.append("timeout")
                    break
                total_attempts += 1
                budget.begin_stage("generate_choices")
                try:
                    budget.consume_call()
                except GenerationBudgetExceeded as exc:
                    raise TavernStoryGenerationError(
                        "生成行动选项时已用完整回合时间预算",
                        failure_kinds=("timeout",),
                    ) from exc
                timeout = budget.per_call_timeout()
                if timeout <= 0:
                    raise TavernStoryGenerationError(
                        "生成行动选项时已用完整回合时间预算",
                        failure_kinds=("timeout",),
                    )
                try:
                    response = await asyncio.wait_for(
                        self._llm_generate_metered(
                            session_id=str(session.get("id") or ""),
                            request_type=(
                                request_type
                                if attempt == 0
                                else request_type + "_repair"
                            ),
                            provider_id=provider_id,
                            prompt=current_prompt,
                            system_prompt_value=choice_system,
                            temperature=config.temperature,
                            max_tokens=min(config.max_tokens, 1200),
                        ),
                        timeout=timeout,
                    )
                except TimeoutError:
                    failure_kinds.append("timeout")
                    budget.record(
                        stage="generate_choices",
                        provider_id=provider_id,
                        attempt=attempt,
                        result="timeout",
                    )
                    failures.append(f"{provider_id}：请求超时")
                    await self.database.record_provider_result(
                        provider_id,
                        success=False,
                        reason="选项生成请求超时",
                    )
                    timed_out = True
                    break
                except Exception as exc:
                    failure_kind = _provider_failure_kind(exc)
                    failure_kinds.append(failure_kind)
                    budget.record(
                        stage="generate_choices",
                        provider_id=provider_id,
                        attempt=attempt,
                        result="fallback",
                    )
                    failures.append(
                        f"{provider_id}：{type(exc).__name__}"
                    )
                    await self.database.record_provider_result(
                        provider_id,
                        success=False,
                        reason=(
                            "选项生成调用失败："
                            + _provider_failure_label(failure_kind)
                        ),
                    )
                    timed_out = True
                    break
                raw = str(
                    getattr(response, "completion_text", "") or ""
                )
                try:
                    payload = extract_json_object(raw)
                    choices = normalize_model_choices(
                        payload.get("choices", payload.get("next_choices")), world
                    )
                    choices = self._validate_choices_for_actor(
                        choices,
                        expected_actor=participant,
                        roster=roster,
                    )
                    await self.database.record_provider_result(
                        provider_id,
                        success=True,
                    )
                    budget.record(
                        stage="generate_choices",
                        provider_id=provider_id,
                        attempt=attempt,
                        result="ok",
                    )
                    return choices, {
                        "provider_class": (
                            "primary" if provider_index == 0 else "backup"
                        ),
                        "repair_count": 1 if attempt > 0 else 0,
                    }
                except (TypeError, ValueError) as exc:
                    budget.record(
                        stage="repair_or_validate",
                        provider_id=provider_id,
                        attempt=attempt,
                        result="repair",
                    )
                    last_error = str(exc)
                    if attempt + 1 >= attempts:
                        break
                    current_prompt = choice_repair_prompt(
                        raw,
                        last_error,
                        world=world,
                        participant=participant,
                    )
            if not timed_out:
                failure_kinds.append("invalid_response")
                failures.append(
                    f"{provider_id}：结构校验失败"
                    f"（{last_error or '未知错误'}）"
                )
                await self.database.record_provider_result(
                    provider_id,
                    success=False,
                    reason=(
                        "选项生成结构校验失败："
                        f"{last_error or '未知错误'}"
                    ),
                )
        raise TavernStoryGenerationError(
            "未能生成一组合法的新选项："
            + ("；".join(failures) or "没有可用模型"),
            failure_kinds=failure_kinds or ("unavailable",),
        )

    async def _generate_resolution(
        self,
        *,
        session_id: str,
        request_type: str,
        world: Mapping[str, Any],
        provider_ids: list[str],
        system: str,
        prompt: str,
        config: TavernConfig,
        expected_actor: Mapping[str, Any] | None = None,
        roster: Sequence[Mapping[str, Any]] = (),
        enforce_mobile_limits: bool = False,
        narrative_mode: object = None,
        player_input: str = "",
        previous_narrative: str = "",
        opening: bool = False,
        budget: TurnGenerationBudget | None = None,
    ) -> tuple[Resolution, str]:
        budget = budget or self._new_generation_budget(config)
        selected_mode = normalize_narrative_mode(narrative_mode)
        (
            speaker_labels,
            player_speaker_refs,
            player_labels,
            speaker_rows,
        ) = self._narrative_speaker_contract(session_id, world, roster)
        dialogue_expected = any(
            not bool(item.get("player")) for item in speaker_rows
        )
        prompt = (
            prompt
            + "\n\n<narrative_document_contract>\n"
            + json.dumps(
                {
                    "mode": selected_mode,
                    "allowed_speakers": speaker_rows,
                    "dialogue_expected": dialogue_expected,
                    "player_speaker_rule": (
                        "player=true 只可逐字引用本轮输入，并设置 quoted_input=true"
                    ),
                    "speaker_rule": (
                        "dialogue/reaction 的 speaker.actor_ref 与 label 必须逐字复制 "
                        "allowed_speakers 中同一项；不得留空、改写或自造 actor_ref"
                    ),
                    "non_dialogue_speaker_rule": (
                        "narration/action/transition/reveal/system_note 的 speaker 必须为 null"
                    ),
                    "visibility": "public",
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n</narrative_document_contract>"
        )
        attempts = config.json_repair_attempts + 1
        original_prompt = prompt
        failures: list[str] = []
        failure_kinds: list[str] = []
        total_attempts = 0
        for provider_index, provider_id in enumerate(provider_ids):
            if provider_index > 0 and not budget.consume_fallback():
                break
            current_prompt = prompt
            last_error = ""
            provider_failed = False
            for attempt in range(attempts):
                if attempt > 0 and not budget.consume_repair():
                    break
                if total_attempts >= _MAX_TOTAL_MODEL_ATTEMPTS:
                    failures.append(
                        f"{provider_id}：达到全局模型重试上限"
                    )
                    failure_kinds.append("timeout")
                    provider_failed = True
                    break
                total_attempts += 1
                budget.begin_stage("generate_narrative")
                try:
                    budget.consume_call()
                except GenerationBudgetExceeded as exc:
                    raise TavernStoryGenerationError(
                        "生成故事正文时已用完整回合时间预算",
                        failure_kinds=("timeout",),
                    ) from exc
                timeout = budget.per_call_timeout()
                if timeout <= 0:
                    raise TavernStoryGenerationError(
                        "生成故事正文时已用完整回合时间预算",
                        failure_kinds=("timeout",),
                    )
                try:
                    response = await asyncio.wait_for(
                        self._llm_generate_metered(
                            session_id=session_id,
                            request_type=(
                                request_type
                                if attempt == 0
                                else request_type + "_repair"
                            ),
                            provider_id=provider_id,
                            prompt=current_prompt,
                            system_prompt_value=system,
                            temperature=config.temperature,
                            max_tokens=config.max_tokens,
                        ),
                        timeout=timeout,
                    )
                except TimeoutError:
                    failure_kinds.append("timeout")
                    budget.record(
                        stage="generate_narrative",
                        provider_id=provider_id,
                        attempt=attempt,
                        result="timeout",
                    )
                    failures.append(f"{provider_id}：请求超时")
                    await self.database.record_provider_result(
                        provider_id,
                        success=False,
                        reason="叙事请求超时",
                    )
                    provider_failed = True
                    break
                except Exception as exc:
                    failure_kind = _provider_failure_kind(exc)
                    failure_kinds.append(failure_kind)
                    budget.record(
                        stage="generate_narrative",
                        provider_id=provider_id,
                        attempt=attempt,
                        result="fallback",
                    )
                    failures.append(
                        f"{provider_id}：{type(exc).__name__}"
                    )
                    await self.database.record_provider_result(
                        provider_id,
                        success=False,
                        reason=(
                            "叙事调用失败："
                            + _provider_failure_label(failure_kind)
                        ),
                    )
                    provider_failed = True
                    break

                raw = str(
                    getattr(response, "completion_text", "") or ""
                )
                try:
                    payload = extract_json_object(raw)
                    core_payload = dict(payload)
                    raw_choices = core_payload.pop("next_choices", None)
                    resolution = validate_resolution(
                        core_payload,
                        narrative_mode=selected_mode,
                        narrative_options={
                            "dialogue_expected": dialogue_expected,
                            "opening": bool(opening),
                            "max_output_chars": config.max_output_chars,
                            "allowed_speaker_refs": speaker_labels,
                            "player_actor_refs": player_speaker_refs,
                            "player_labels": player_labels,
                            "player_input": player_input,
                            "previous_narrative": previous_narrative,
                        },
                    )
                    if enforce_mobile_limits:
                        resolution = self._validate_mobile_resolution(
                            resolution,
                            expected_actor=expected_actor,
                            roster=roster,
                            narrative_mode=narrative_mode,
                        )
                    choice_error = ""
                    normalized_choices: list[dict[str, Any]] = []
                    if resolution.mode == "resolve" and raw_choices is not None:
                        try:
                            normalized_choices = normalize_model_choices(
                                raw_choices, world
                            )
                            if enforce_mobile_limits:
                                normalized_choices = (
                                    self._validate_choices_for_actor(
                                        normalized_choices,
                                        expected_actor=expected_actor,
                                        roster=roster,
                                    )
                                )
                        except (TypeError, ValueError) as exc:
                            choice_error = str(exc)
                    raw_payload = dict(payload)
                    if choice_error:
                        raw_payload["_next_choices_error"] = choice_error
                    resolution = replace(
                        resolution,
                        next_choices=tuple(normalized_choices),
                        raw=raw_payload,
                    )
                    await self.database.record_provider_result(
                        provider_id,
                        success=True,
                    )
                    budget.record(
                        stage="generate_narrative",
                        provider_id=provider_id,
                        attempt=attempt,
                        result="ok",
                    )
                    return resolution, provider_id
                except (TypeError, ValueError) as exc:
                    budget.record(
                        stage="repair_or_validate",
                        provider_id=provider_id,
                        attempt=attempt,
                        result="repair",
                    )
                    last_error = str(exc)
                    if attempt + 1 >= attempts:
                        break
                    current_prompt = repair_prompt(
                        raw,
                        last_error,
                        original_prompt,
                    )
            if not provider_failed:
                failure_kinds.append("invalid_response")
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
        raise TavernStoryGenerationError(
            f"全部叙事模型均未完成本轮：{summary}",
            failure_kinds=failure_kinds or ("unavailable",),
        )

    @staticmethod
    def _new_generation_budget(config: TavernConfig) -> TurnGenerationBudget:
        return TurnGenerationBudget(
            total_seconds=config.generation_budget_total_seconds,
            max_calls=config.generation_budget_max_calls,
            per_call_seconds=config.generation_budget_per_call_seconds,
            max_fallbacks=config.generation_budget_max_fallbacks,
            repair_budget=config.json_repair_attempts,
            reserve_seconds=5.0,
        )
