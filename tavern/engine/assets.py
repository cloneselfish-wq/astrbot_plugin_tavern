from .shared import *
from .errors import *
from ..copy.entities import decorate_entity

class AssetOperationMixin:
    @staticmethod
    def _decorate_story_document(
        document: NarrativeDocument,
        *,
        resolution: Resolution,
        world: Mapping[str, Any],
        roster: Sequence[Mapping[str, Any]],
        session_characters: Sequence[Mapping[str, Any]],
    ) -> NarrativeDocument:
        catalog = build_story_entity_catalog(
            world,
            roster,
            session_characters,
        )
        return replace(
            document,
            blocks=tuple(
                replace(
                    block,
                    text=decorate_story_entities(
                        block.text,
                        mentions=resolution.entity_mentions,
                        catalog=catalog,
                    ),
                )
                for block in document.blocks
            ),
        )

    @staticmethod
    def _decorate_story_narrative(
        narrative: str,
        *,
        resolution: Resolution,
        world: Mapping[str, Any],
        roster: Sequence[Mapping[str, Any]],
        session_characters: Sequence[Mapping[str, Any]],
    ) -> str:
        catalog = build_story_entity_catalog(
            world,
            roster,
            session_characters,
        )
        return decorate_story_entities(
            narrative,
            mentions=resolution.entity_mentions,
            catalog=catalog,
        )

    async def _stage_item_ops(
        self,
        *,
        session_id: str,
        ops: Any,
        operation_prefix: str,
        participant: Mapping[str, Any] | None,
        actor_id: str,
        source: str = "story",
        recovery: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Validate model item operations without mutating item_instances."""

        if not isinstance(ops, list) or not ops:
            return []
        instance = await self.database.get_instance_config(session_id)
        world = (
            instance.get("world_snapshot")
            if isinstance(instance, Mapping)
            else {}
        )
        if not isinstance(world, Mapping):
            world = {}
        from ..item_catalog import (
            item_label,
            resolve_item_ref,
        )

        default_owner = (
            self._actor_owner_key(participant)
            if isinstance(participant, Mapping)
            else ""
        )

        def owner(
            value: Any,
            *,
            required: bool = True,
            allow_default: bool = True,
        ) -> str:
            raw = str(value or "").strip()
            if raw.casefold() in {"self", "自己", "我"}:
                raw = default_owner
            elif not raw and allow_default:
                raw = default_owner
            if required and not raw:
                raise TavernEngineError(
                    "模型生成的物品操作缺少角色目标，本轮没有提交"
                )
            return raw

        staged: list[dict[str, Any]] = []
        for index, raw in enumerate(ops[:30]):
            if not isinstance(raw, Mapping):
                raise TavernEngineError(
                    "模型生成了格式无效的物品操作，本轮没有提交"
                )
            kind = str(raw.get("op") or "").strip().lower()
            if kind in {"prop", "mention"}:
                # Narrative-only objects intentionally never reach the
                # persistent item operation transaction.
                continue
            if kind not in {"grant", "consume", "transfer"}:
                raise TavernEngineError(
                    f"模型生成了不支持的物品操作：{kind or '（空）'}"
                )
            item_ref = str(
                raw.get("item_ref")
                or raw.get("item_id")
                or raw.get("item")
                or ""
            ).strip()
            item_id = resolve_item_ref(world, item_ref)
            if not item_id:
                raise TavernEngineError(
                    "无法提交本轮物品变更：模型返回的物品无法匹配"
                    "当前世界候选目录。\n"
                    "系统已执行一次本地结构化规范化，但没有找到唯一合法实体；"
                    "世界状态、检定结果和物品变更均未提交。\n"
                    "请发送\n/团 重试\n"
                )
            if recovery is not None and item_ref not in {
                item_id,
                f"item:{item_id}",
            }:
                recovery.setdefault("repairs", []).append(
                    {
                        "kind": "item_ref_normalized",
                        "input_label": item_ref,
                        "resolved_label": item_label(world, item_id),
                    }
                )
                recovery["repair_count"] = 1
            try:
                quantity = int(raw.get("quantity", 1) or 1)
            except (TypeError, ValueError) as exc:
                raise TavernEngineError("物品数量必须是整数") from exc
            if not 1 <= quantity <= 100:
                raise TavernEngineError("物品数量必须在 1 到 100 之间")
            operation_id = f"{operation_prefix}:item:{index}"
            reason = str(raw.get("reason") or f"{source}:{kind}")[:300]
            if kind == "grant":
                staged.append(
                    {
                        "op": "grant",
                        "grant": {
                            "owner_type": str(
                                raw.get("owner_type") or "character"
                            ),
                            "owner_ref": owner(raw.get("owner_ref")),
                            "item_id": item_id,
                            "quantity": quantity,
                            "container": str(raw.get("container") or ""),
                            "source": source,
                            "state": (
                                dict(raw.get("state") or {})
                                if isinstance(raw.get("state"), Mapping)
                                else {}
                            ),
                        },
                        "operation_id": operation_id,
                        "actor_id": actor_id or "system",
                    }
                )
            elif kind == "consume":
                staged.append(
                    {
                        "op": "consume",
                        "owner_ref": owner(raw.get("owner_ref")),
                        "items": {item_id: quantity},
                        "reason": reason,
                        "operation_id": operation_id,
                    }
                )
            else:
                staged.append(
                    {
                        "op": "transfer",
                        "from_owner": owner(raw.get("from_owner")),
                        "to_owner": owner(
                            raw.get("to_owner"),
                            required=True,
                            allow_default=False,
                        ),
                        "item_id": item_id,
                        "quantity": quantity,
                        "reason": reason,
                        "operation_id": operation_id,
                    }
                )
        return staged

    @staticmethod
    def _entity_recovery_receipt(
        recovery: Mapping[str, Any],
        operation_id: str,
    ) -> dict[str, Any] | None:
        repairs = [
            dict(item)
            for item in recovery.get("repairs") or []
            if isinstance(item, Mapping)
        ]
        if not repairs:
            return None
        trace_id = hashlib.sha256(
            str(operation_id or "").encode("utf-8")
        ).hexdigest()[:8].upper()
        return {
            "schema": "tavern-entity-recovery-receipt/1.0.0-rc10",
            "status": "repaired",
            "repair_count": 1,
            "repairs": repairs,
            "message": (
                "系统已把一个物品显示名规范化为世界目录中的唯一实体；"
                "持久物品变更仍与本轮世界事实原子提交。"
            ),
            "trace_id": trace_id,
        }

    async def _stage_economy_ops(
        self,
        *,
        session_id: str,
        ops: Any,
        operation_prefix: str,
        actor_id: str,
        source: str = "story",
    ) -> list[dict[str, Any]]:
        """C6：校验模型提议的经济操作并生成待提交计划（不写库）。

        回合提交时由 commit_turn 在同一事务内应用；未启用经济时返回空。
        """
        if not isinstance(ops, list) or not ops:
            return []
        try:
            state = await self.database.economy_state(session_id)
        except Exception:
            return []
        if not state.get("enabled"):
            return []
        staged: list[dict[str, Any]] = []
        for index, op in enumerate(ops):
            if not isinstance(op, Mapping):
                continue
            kind = str(op.get("kind") or "adjust")
            currency_id = str(op.get("currency_id") or "")
            if not kind or not currency_id:
                raise TavernEngineError(
                    "模型生成了不完整的经济操作，本轮没有提交"
                )
            if not op.get("from_owner_type") and not op.get("to_owner_type"):
                raise TavernEngineError(
                    "模型生成的经济操作缺少钱包方向，本轮没有提交"
                )
            staged.append(
                {
                    "operation_id": f"{operation_prefix}:econ:{index}",
                    "kind": kind,
                    "currency_id": currency_id,
                    "amount": op.get("amount"),
                    "from_owner_type": str(op.get("from_owner_type") or ""),
                    "from_owner_ref": str(op.get("from_owner_ref") or ""),
                    "to_owner_type": str(op.get("to_owner_type") or ""),
                    "to_owner_ref": str(op.get("to_owner_ref") or ""),
                    "reason": str(op.get("reason") or ""),
                    "source": source,
                    "actor_id": actor_id,
                    "target_ref": str(op.get("target_ref") or ""),
                }
            )
        return staged

    # ── 1.0.0-A7：道具 / 技能 / 转赠 / 商店 / 购买 ──────────────────
    @staticmethod
    def _is_medical_item(name: str) -> bool:
        """粗略识别医疗类道具/技能（用于重创恢复判定）。"""
        text = str(name or "").casefold()
        markers = ("医疗", "绷带", "草药", "急救", "包扎", "治疗", "医", "药", "bandage", "heal", "herb", "medic")
        return any(marker in text for marker in markers)

    async def _preflight_actor(
        self,
        session_id: str,
        sender_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
        """校验运行状态 + 轮到该玩家 + 返回 (session, turn, roster, participant)。"""
        session = await self.database.get_session(session_id)
        if session["state"] != "running":
            raise TavernEngineError("酒馆当前不在运行状态")
        turn = await self.database.get_turn_status(session_id)
        if turn["current_user_id"] and turn["current_user_id"] != sender_id:
            current_name = str(turn.get("current_name") or "").strip()
            if not current_name:
                raise TavernTurnOrderError(
                    "当前行动者缺少可公开显示的角色名称，"
                    "系统无法安全确认行动顺序；本条操作未记录。",
                    turn=turn,
                )
            current = decorate_entity("character", current_name)
            raise TavernTurnOrderError(
                f"当前轮到 {current}，本条操作未记录。",
                turn=turn,
            )
        roster = await self.database.list_roster(session_id)
        participant = next(
            (
                item
                for item in roster
                if str(item.get("group_user_id") or "") == sender_id
            ),
            None,
        )
        if not participant:
            raise TavernEngineError("你尚未加入当前副本")
        return session, turn, roster, participant

    async def _has_item_instance(
        self,
        session_id: str,
        participant: Mapping[str, Any],
        item_name: str,
    ) -> bool:
        """C6：持有判断只以 item_instances 为权威，不再回退旧名称背包。"""
        item_id = await self._resolve_item_id(session_id, item_name)
        instances = await self.database.list_item_instances(
            session_id,
            self._actor_owner_key(participant),
        )
        return any(
            str(instance.get("item_id")) == item_id
            and int(instance.get("quantity") or 0) > 0
            for instance in instances
        )

    async def _resolve_item_id(
        self,
        session_id: str,
        item_name: str,
    ) -> str:
        """把显示名解析为稳定物品 ID；无目录时回退为原名称。"""
        try:
            instance = await self.database.get_instance_config(session_id)
            world = instance.get("world_snapshot") if isinstance(instance, Mapping) else {}
            if isinstance(world, Mapping):
                from ..item_catalog import resolve_item_ref

                resolved = resolve_item_ref(world, item_name)
                if resolved:
                    return resolved
        except Exception:
            pass
        return str(item_name or "").strip()

    def _actor_owner_key(
        self,
        participant: Mapping[str, Any],
    ) -> str:
        """当前行动角色在结构化物品与钱包中的规范 owner 键。"""
        keys = self._actor_owner_keys(participant)
        return next((key for key in keys if key), str(participant.get("id") or ""))

    async def use_item(
        self,
        *,
        event: Any,
        session_id: str,
        sender_id: str,
        sender_name: str,
        item_name: str,
        target_ref: str = "",
        progress: ProgressCallback | None = None,
    ) -> EngineReply:
        """道具动作：校验拥有 → 生成故事 → 提交时同事务消耗 1 件。"""
        config = self.config_provider()
        item_name = clean_text(item_name, max_chars=100)
        if not item_name:
            raise TavernEngineError(
                "请指定要使用的道具，例如："
                f"{config.trigger_prefix} 道具 绷带"
            )
        _session, turn, _roster, participant = await self._preflight_actor(
            session_id, sender_id
        )
        if not await self._has_item_instance(session_id, participant, item_name):
            raise TavernEngineError(f"你的背包里没有「{item_name}」")
        owner = self._actor_owner_key(participant)
        item_id = await self._resolve_item_id(session_id, item_name)
        item_ops: list[dict[str, Any]] = [
            {
                "op": "consume",
                "owner_ref": owner,
                "items": {item_id: 1},
                "reason": f"使用道具：{item_name}",
                "operation_id": operation_key(
                    session_id,
                    "use_item",
                    turn_no=int(turn.get("round_no") or 0),
                    actor_id=owner,
                    source_id=item_name,
                    payload={"item": item_name, "target": target_ref},
                ),
            }
        ]
        if target_ref and self._is_medical_item(item_name):
            item_ops.append(
                {
                    "op": "remove_status",
                    "target_ref": target_ref,
                    "keywords": (
                        "重创",
                        "重伤",
                        "无法行动",
                        "倒下",
                        "incapacitated",
                        "down",
                        "unconscious",
                    ),
                    "actor_id": sender_id,
                    "reason": f"use_item:{item_name}",
                }
            )
        content = f"使用背包道具「{item_name}」"
        if target_ref:
            content += f"对 {target_ref}"
        reply = await self.process(
            event=event,
            session_id=session_id,
            sender_id=sender_id,
            sender_name=sender_name,
            content=content,
            progress=progress,
            item_ops=item_ops,
        )
        removed = list(
            (
                (reply.session or {}).get("asset_effects") or {}
            ).get("status_removed") or []
        )
        prefix_lines = [f"✋ 使用道具：{item_name}（已消耗 1 件）"]
        if removed:
            prefix_lines.append(
                f"❤️ 已解除 {target_ref} 的「{removed[0]}」状态"
            )
        prefix = "\n".join(prefix_lines) + "\n\n"
        return replace(
            reply,
            story_text=prefix + reply.story_text,
            text=prefix + reply.text,
        )

    async def use_skill(
        self,
        *,
        event: Any,
        session_id: str,
        sender_id: str,
        sender_name: str,
        skill_name: str,
        target_ref: str = "",
        action_note: str = "",
        progress: ProgressCallback | None = None,
    ) -> EngineReply:
        """技能动作：校验掌握 → 生成故事 → 再给选项。"""
        config = self.config_provider()
        skill_name = clean_text(skill_name, max_chars=100)
        if not skill_name:
            raise TavernEngineError(
                "请指定要使用的技能，例如："
                f"{config.trigger_prefix} 技能 急救包扎"
            )
        _session, turn, _roster, participant = await self._preflight_actor(
            session_id, sender_id
        )
        profile = participant.get("card_profile") or {}
        if not isinstance(profile, Mapping):
            profile = participant.get("profile") or {}
        instance = await self.database.get_instance_config(session_id)
        world = (
            instance.get("world_snapshot")
            if isinstance(instance, Mapping)
            else {}
        )
        ability_values = actor_values_for_roles(
            world if isinstance(world, Mapping) else {},
            profile,
            ("actor.capability.list",),
        )
        abilities = ability_values.get("actor.capability.list", "")
        if abilities and skill_name not in abilities:
            raise TavernEngineError(
                f"你的角色卡没有「{skill_name}」技能（现有：{abilities[:80]}）"
            )
        # C6：治疗类技能的状态解除不再提前落库；作为 remove_status
        # 计划随回合同事务提交，模型/质量/修订失败时整笔回滚，玩家
        # 不会看到“状态已解除但回合没有推进”的半提交结果。
        item_ops: list[dict[str, Any]] = []
        if target_ref and self._is_medical_item(skill_name):
            item_ops.append(
                {
                    "op": "remove_status",
                    "target_ref": target_ref,
                    "keywords": (
                        "重创",
                        "重伤",
                        "无法行动",
                        "倒下",
                        "incapacitated",
                        "down",
                        "unconscious",
                    ),
                    "actor_id": sender_id,
                    "reason": f"use_skill:{skill_name}",
                }
            )
        content = f"使用技能「{skill_name}」"
        if target_ref:
            content += f"对 {target_ref}"
        if action_note:
            content += f" {action_note}"
        reply = await self.process(
            event=event,
            session_id=session_id,
            sender_id=sender_id,
            sender_name=sender_name,
            content=content,
            progress=progress,
            item_ops=item_ops or None,
        )
        removed = list(
            (
                (reply.session or {}).get("asset_effects") or {}
            ).get("status_removed") or []
        )
        prefix_lines = [f"⚡ 使用技能：{skill_name}"]
        if removed:
            prefix_lines.append(
                f"❤️ 已解除 {target_ref} 的「{removed[0]}」状态"
            )
        prefix = "\n".join(prefix_lines) + "\n\n"
        return replace(
            reply,
            story_text=prefix + reply.story_text,
            text=prefix + reply.text,
        )

    async def give_item(
        self,
        *,
        session_id: str,
        sender_id: str,
        item_name: str,
        target_ref: str,
    ) -> str:
        """/团 赠予 <道具> <目标>：权威移动 1 件 + 审计。"""
        item_name = clean_text(item_name, max_chars=100)
        target_ref = clean_text(target_ref, max_chars=128)
        if not item_name or not target_ref:
            raise TavernEngineError("用法：/团 赠予 <道具> <目标>")
        session = await self.database.get_session(session_id)
        if session["state"] != "running":
            raise TavernEngineError("酒馆当前不在运行状态，无法转赠道具")
        roster = await self.database.list_roster(session_id)
        participant = next(
            (
                item
                for item in roster
                if str(item.get("group_user_id") or "") == sender_id
            ),
            None,
        )
        if not participant:
            raise TavernEngineError("你尚未加入当前副本")
        if not await self._has_item_instance(session_id, participant, item_name):
            raise TavernEngineError(f"你的背包里没有「{item_name}」")
        target = None
        target_key_lower = target_ref.casefold()
        for member in roster:
            candidates = [
                str(member.get("id") or ""),
                str(member.get("group_user_id") or ""),
                str(member.get("user_id") or ""),
                str(member.get("character_name") or ""),
                str(member.get("character_code") or ""),
                str(member.get("display_name") or ""),
            ]
            if any(item.casefold() == target_key_lower for item in candidates):
                target = member
                break
        if not target:
            names = "、".join(
                str(member.get("character_name") or member.get("display_name"))
                for member in roster
                if str(member.get("participation_status") or "") in {"active", "standby", "away"}
            ) or "（暂无在场角色）"
            return (
                f"🎁 未找到转赠目标「{target_ref}」。\n"
                f"可用目标：{names}\n"
                f"发送：/团 赠予 {item_name} <目标>"
            )
        giver = self._actor_owner_key(participant)
        receiver = self._actor_owner_key(target)
        if giver == receiver:
            raise TavernEngineError("不能把道具转赠给自己")
        operation_id = operation_key(
            session_id,
            "give_item",
            turn_no=int(session.get("turn_no") or 0),
            actor_id=giver,
            source_id=f"{item_name}->{receiver}",
            payload={"item": item_name, "receiver": receiver},
        )
        item_id = await self._resolve_item_id(session_id, item_name)
        await self.database.transfer_item_instances(
            session_id=session_id,
            from_owner=giver,
            to_owner=receiver,
            item_id=item_id,
            quantity=1,
            reason=f"转赠 {item_name} 给 {target.get('character_name') or target.get('display_name')}",
            operation_id=operation_id,
        )
        target_name = target.get("character_name") or target.get("display_name")
        sender_name = participant.get("character_name") or participant.get("display_name")
        return (
            f"🎁 {sender_name} 将 {item_name} ×1 转交给 {target_name}。\n"
            f"🎒 {target_name} 背包：{item_name} +1"
        )

    async def shop_list(self, *, session_id: str) -> str:
        """/团 商店：展示当前场景可用的运行态集市报价。"""
        session = await self.database.get_session(session_id)
        try:
            instance = await self.database.get_instance_config(session_id)
        except Exception:
            instance = {}
        world = instance.get("world_snapshot") if isinstance(instance, Mapping) else {}
        if not isinstance(world, Mapping):
            raise TavernEngineError("无法读取当前世界包")
        economy = world_contract(world).get("economy") or {}
        shops = [
            item
            for item in (economy.get("shops") or [])
            if isinstance(item, Mapping)
        ]
        if not economy.get("available") or not shops:
            return "🛒 当前世界未启用经济模块，因此没有可用集市。"
        from ..protocol.runtime import flatten_runtime, runtime_from_state

        runtime = flatten_runtime(
            runtime_from_state(session.get("world_state") or {})
        )
        views = [
            project_market_view(
                world=world,
                runtime=runtime,
                shop_ref=str(shop.get("shop_id") or ""),
            )
            for shop in shops
        ]
        view = next(
            (item for item in views if item.get("available")),
            views[0] if views else {},
        )
        if not view.get("available"):
            reason = str(view.get("blocked_reason") or "当前场景没有开放的商店")
            return (
                "🛒 当前无法进入集市。\n"
                f"原因：{reason}\n"
                "下一步：完成当前场景目标或转入有商店的地点后，再发送 /团 商店。"
            )
        offers = [
            item
            for item in (view.get("offers") or [])
            if isinstance(item, Mapping)
        ]
        lines = [f"🛒 {view.get('shop_label') or '集市'}"]
        for index, item in enumerate(offers[:8]):
            letter = chr(ord("A") + index)
            name = str(item.get("item_label") or "")
            description = str(item.get("description") or "")
            price = (
                item.get("price")
                if isinstance(item.get("price"), Mapping)
                else {}
            )
            price_text = (
                f"{price.get('amount')} "
                f"{price.get('currency_label') or price.get('currency_id')}"
            )
            lines.extend(
                [
                    f"{letter}. 『{name}』",
                    description or "商家没有提供更多说明。",
                    f"价格：{price_text}（库存 {int(item.get('stock', 0) or 0)}）",
                ]
            )
            reasons = "；".join(
                str(value)
                for value in (item.get("change_reasons") or [])
                if str(value)
            )
            if reasons:
                lines.append(f"变动原因：{reasons}")
            lines.append("")
        lines.append("💬 发送：/团 购买 A（或输入完整商品名）")
        return "\n".join(lines)

    async def buy_item(
        self,
        *,
        session_id: str,
        sender_id: str,
        item_ref: str,
    ) -> str:
        """/团 购买 <商品>：校验余额 → 扣款 → 入包。"""
        item_ref = clean_text(item_ref, max_chars=100)
        if not item_ref:
            raise TavernEngineError("用法：/团 购买 <商品>")
        session = await self.database.get_session(session_id)
        if session["state"] != "running":
            raise TavernEngineError("酒馆当前不在运行状态，无法购买")
        roster = await self.database.list_roster(session_id)
        participant = next(
            (
                item
                for item in roster
                if str(item.get("group_user_id") or "") == sender_id
            ),
            None,
        )
        if not participant:
            raise TavernEngineError("你尚未加入当前副本")
        try:
            instance = await self.database.get_instance_config(session_id)
        except Exception:
            instance = {}
        world = instance.get("world_snapshot") if isinstance(instance, Mapping) else {}
        if not isinstance(world, Mapping):
            raise TavernEngineError("无法读取当前世界包")
        economy = world_contract(world).get("economy") or {}
        shops = [
            item
            for item in (economy.get("shops") or [])
            if isinstance(item, Mapping)
        ]
        if not economy.get("available") or not shops:
            raise TavernEngineError("当前世界未开放商店")
        from ..protocol.runtime import flatten_runtime, runtime_from_state

        runtime = flatten_runtime(
            runtime_from_state(session.get("world_state") or {})
        )
        views = [
            project_market_view(
                world=world,
                runtime=runtime,
                shop_ref=str(shop.get("shop_id") or ""),
            )
            for shop in shops
        ]
        market = next(
            (item for item in views if item.get("available")),
            None,
        )
        if not market:
            raise TavernEngineError(
                "当前场景没有开放的商店；请先完成场景目标或转入集市地点。"
            )
        items = [
            item
            for item in (market.get("offers") or [])
            if isinstance(item, Mapping)
        ]
        selected: Mapping[str, Any] | None = None
        if len(item_ref) == 1 and item_ref.upper() in "ABCDEFGH":
            index = ord(item_ref.upper()) - ord("A")
            if index < len(items):
                selected = items[index]
        if selected is None:
            lowered = item_ref.casefold()
            for item in items:
                name = str(item.get("item_label") or "")
                if name.casefold() == lowered:
                    selected = item
                    break
        if selected is None:
            available = "、".join(
                str(item.get("item_label") or "")
                for item in items[:8]
            )
            return (
                f"🛒 未找到商品「{item_ref}」。\n"
                f"当前可购买：{available}\n"
                "下一步：发送 /团 商店 查看当前序号。"
            )
        state = await self.database.economy_state(session_id)
        if not state.get("enabled"):
            raise TavernEngineError("经济系统未启用，暂时无法购买")
        price = (
            selected.get("price")
            if isinstance(selected.get("price"), Mapping)
            else {}
        )
        amount = str(price.get("amount") or "")
        currency_id = str(price.get("currency_id") or "")
        if not amount or not currency_id:
            raise TavernEngineError("服务器未能生成有效报价，请重新查看集市")
        owner = self._actor_owner_key(participant)
        operation_id = operation_key(
            session_id,
            "buy_item",
            turn_no=int(session.get("revision") or 0),
            actor_id=owner,
            source_id=str(selected.get("offer_id") or ""),
            payload={
                "quote_id": str(selected.get("quote_id") or ""),
                "stock_revision": int(selected.get("stock_revision", 0) or 0),
            },
        )
        item_name = str(selected.get("item_label") or "")
        paid = await self.database.atomic_purchase(
            session_id=session_id,
            operation_id=operation_id,
            owner=owner,
            owner_type="character",
            shop_ref=str(market.get("shop_ref") or ""),
            offer_id=str(selected.get("offer_id") or ""),
            quantity=1,
            quote_id=str(selected.get("quote_id") or ""),
            expected_price_revision=int(
                selected.get("price_revision", 0) or 0
            ),
            expected_stock_revision=int(
                selected.get("stock_revision", 0) or 0
            ),
            actor_id=sender_id,
            reason=f"购买『{item_name}』",
        )
        if not paid.get("ok"):
            return (
                "💸 购买失败。\n"
                f"原因：{paid.get('message') or '服务器未能确认报价'}\n"
                "系统未完成扣款或发货。\n"
                "下一步：发送 /团 商店 获取最新报价后重试。"
            )
        currency_label = str(price.get("currency_label") or currency_id)
        new_balance = await self.database.economy_balance(
            session_id, "character", owner, currency_id
        )
        return (
            f"💰 已支付 {paid.get('amount_major')} {currency_label}。\n"
            f"🎒 『{item_name}』×1 已收入个人背包。\n"
            f"集市剩余库存：{paid.get('remaining_stock')}\n"
            f"当前资金：{new_balance.get('balance_major')} {currency_label}"
        )

    @staticmethod
    def _is_incapacitated(statuses: Any) -> bool:
        """1.0.0-A7：判断状态列表是否含“重创/无法行动”条目。"""
        if not isinstance(statuses, list):
            return False
        markers = ("重创", "重伤", "无法行动", "倒下", "incapacitated", "down", "unconscious")
        for raw in statuses:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "").casefold()
            effect = str(raw.get("effect") or "").casefold()
            if any(marker in name for marker in markers) or any(
                marker in effect for marker in markers
            ):
                return True
        return False

    @staticmethod
    def _actor_owner_keys(participant: Mapping[str, Any]) -> list[str]:
        """当前行动角色在结构化物品与钱包中的候选 owner 引用。"""
        keys: list[str] = []
        for value in (
            participant.get("id"),
            participant.get("group_user_id"),
            participant.get("user_id"),
            participant.get("character_name"),
            participant.get("character_code"),
            participant.get("display_name"),
        ):
            text = str(value or "").strip()
            if text and text not in keys:
                keys.append(text)
        return keys
