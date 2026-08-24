from .common import *
from .sessions import *
from .characters import *

def format_card_preview(draft: Mapping[str, Any]) -> str:
    template = draft.get("template") or {}
    fields = draft.get("fields") or {}
    lines = ["【角色卡预览】"]
    refs = fields.get("_preset_refs")
    refs = refs if isinstance(refs, Mapping) else {}
    staged = staged_creation(template)

    def append_field(definition: Mapping[str, Any]) -> None:
        if definition.get("stat_key"):
            return
        key = str(definition.get("key") or "")
        raw_value = fields.get(key, "")
        selected = refs.get(key)
        if isinstance(selected, list):
            labels = [
                str(item.get("label") or item.get("id") or "")
                for item in selected
                if isinstance(item, Mapping)
            ]
            value = "、".join(item for item in labels if item)
        elif isinstance(selected, Mapping):
            value = str(
                selected.get("label")
                or selected.get("id")
                or raw_value
                or ""
            )
        else:
            value = (
                "、".join(str(item) for item in raw_value)
                if isinstance(raw_value, list)
                else str(raw_value or "")
            )
        if definition.get("private"):
            if value:
                value = "（私密字段已保存）"
            elif staged:
                value = "（剧情中补充，仅本人可见）"
            else:
                value = "（未填写）"
        entity_type = {
            "name": "character",
            "code": "character",
            "origin_region": "location",
            "hometown": "location",
            "starting_weapon": "item",
            "starting_armor": "item",
            "loadout": "item",
            "signature_item": "item",
            "abilities": "ability",
            "specialties": "ability",
            "specialization": "ability",
            "weakness": "status",
            "faction_affiliation": "faction",
            "personal_goal": "quest",
            "contact": "character",
            "rival": "character",
        }.get(str(definition.get("key") or ""), "")
        if value and not definition.get("private"):
            if "、" in value and entity_type:
                value = "、".join(
                    decorate_entity(entity_type, item)
                    for item in value.split("、")
                )
            else:
                value = decorate_entity(entity_type, value)
        empty_text = (
            "（剧情中补充）"
            if staged and field_stage(definition) != CARD_STAGE_A
            else "（未填写）"
        )
        lines.append(
            f"· {definition.get('label')}：{value or empty_text}"
        )

    if staged:
        for stage in (CARD_STAGE_A, CARD_STAGE_B, CARD_STAGE_C):
            stage_fields = [
                item
                for item in template.get("fields") or []
                if isinstance(item, Mapping)
                and field_stage(item) == stage
            ]
            if not stage_fields:
                continue
            lines.append(f"【{stage_label(template, stage)}】")
            for definition in stage_fields:
                append_field(definition)
            lines.append("")
    else:
        for definition in template.get("fields") or []:
            if isinstance(definition, Mapping):
                append_field(definition)
    resolved_boundaries = fields.get("_resolved_boundaries")
    if isinstance(resolved_boundaries, Mapping):
        knowledge = resolved_boundaries.get("knowledge") or {}
        content = resolved_boundaries.get("content") or {}
        lines.extend(["", "【角色知识范围】"])
        domains = knowledge.get("domains") or {}
        if domains:
            level_labels = {
                "unknown": "未知",
                "rumor": "听闻",
                "basic": "基础",
                "trained": "受训",
                "expert": "精通",
            }
            lines.append(
                "· 已掌握领域："
                + "；".join(
                    f"{key}（{level_labels.get(str(value), value)}）"
                    for key, value in domains.items()
                )
            )
        forbidden = [
            str(item).replace("导演秘密", "主持人保留信息")
            for item in knowledge.get("forbidden_domains") or []
        ]
        lines.append(
            "· 暂不可直接知道：" + "、".join(forbidden)
            if forbidden
            else "· 暂不可直接知道：未公开的幕后信息与他人私密内容"
        )
        lines.extend(["", "【内容安全设置】"])
        rating_labels = {
            "general": "通用级",
            "teen": "青少年级",
            "mature": "成人议题级",
        }
        rating = str(content.get("rating") or "general")
        lines.append(f"· 当前分级：{rating_labels.get(rating, rating)}")
        if content.get("hard_denials"):
            lines.append("· 明确不出现：" + "、".join(content["hard_denials"]))
    if uses_preset_stack_stats(template):
        lines.append("")
        try:
            resolved = calculate_preset_stack_stats(
                template,
                fields,
                require_complete=True,
            )
        except ValueError as exc:
            lines.append(f"【角色数值｜由角色选择自动计算】尚未生成：{exc}")
        else:
            assert resolved is not None
            lines.append(format_preset_stack_result(resolved))
        lines.extend(
            [
                "",
                "确认：/团 确认建卡",
                "重新开始：/团 重新建卡",
                "保留席位并取消草稿：/团 取消建卡",
                "彻底放弃席位：/团 放弃席位 确认",
            ]
        )
        return "\n".join(lines)
    if uses_profession_preset_stats(template):
        lines.append("")
        try:
            resolved = resolve_profession_stats(
                template,
                fields,
                require_complete=False,
            )
        except ValueError as exc:
            lines.append(f"【角色数值】尚未生成：{exc}")
        else:
            lines.append("【角色数值｜职业基础与主副属性】")
            lines.append(f"· 职业：{resolved['profession']}")
            lines.append(
                f"· 主属性：{resolved['primary']['label'] or '（未选）'}"
                f" +{resolved['primary']['bonus']}"
            )
            lines.append(
                f"· 副属性：{resolved['secondary']['label'] or '（未选）'}"
                f" +{resolved['secondary']['bonus']}"
            )
            for key, value in resolved["raw"].items():
                base_value = resolved["base"][key]
                delta = value - base_value
                delta_text = f"（基础{base_value}{delta:+d}）" if delta else ""
                lines.append(
                    f"· {resolved['labels'][key]}：{value}{delta_text}"
                    f"（检定修正 {resolved['modifiers'][key]:+d}）"
                )
            lines.append(
                f"· 总和：基础 {resolved['base_total']}"
                f" + 加成 {resolved['bonus_total']}"
                f" = {resolved['effective_total']}"
            )
        lines.extend(
            [
                "",
                "重新选择主副属性：/团 重填数值",
                "确认：/团 确认建卡",
                "重新开始：/团 重新建卡",
                "保留席位并取消草稿：/团 取消建卡",
                "彻底放弃席位：/团 放弃席位 确认",
            ]
        )
        return "\n".join(lines)
    allocation = card_stat_allocation(template, fields)
    if allocation["stat_fields"]:
        modifier_table = (template.get("stats") or {}).get(
            "modifier_table"
        ) or {}
        lines.append("")
        lines.append("【角色数值】")
        for item in allocation["stat_fields"]:
            value = allocation["values"].get(item["field_key"])
            if value is None:
                lines.append(f"· {item['label']}：（未填写）")
                continue
            modifier = int(modifier_table.get(str(value), 0))
            lines.append(
                f"· {item['label']}：{value}"
                f"（检定修正 {modifier:+d}）"
            )
        lines.append(
            f"· 预算：已使用 {allocation['used']}"
            f"/{allocation['budget']} 点"
            f" · 剩余 {allocation['remaining']} 点"
        )
    lines.extend(
        [
            "",
            "只重新分配数值：/团 重填数值",
            "确认：/团 确认建卡",
            "重新开始：/团 重新建卡",
            "保留席位并取消草稿：/团 取消建卡",
            "彻底放弃席位：/团 放弃席位 确认",
        ]
    )
    return "\n".join(lines)


def _review_reference(participant: Mapping[str, Any]) -> str:
    raw = str(participant.get("id") or "").split("_", 1)[-1]
    token = re.sub(r"[^a-zA-Z0-9]", "", raw).upper()
    return f"R-{(token or 'UNKNOWN')[:8]}"


def _pending_review_cards(
    roster: list[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    return [
        item
        for item in roster
        if item.get("card_status") == "pending_review"
        and item.get("character_version_id")
    ]


def _resolve_pending_review(
    pending: list[Mapping[str, Any]],
    reference: str,
) -> Mapping[str, Any]:
    normalized = str(reference or "").strip()
    ordinal = normalized.removeprefix("#")
    if ordinal.isdigit():
        index = int(ordinal)
        if 1 <= index <= len(pending):
            return pending[index - 1]
        raise DatabaseNotFoundError(
            "待审核序号不存在，请发送 /团 审核 刷新名单"
        )

    lowered = normalized.casefold()
    matches = []
    for item in pending:
        aliases = item.get("aliases")
        aliases = aliases if isinstance(aliases, list) else []
        candidates = {
            str(item.get("id") or ""),
            _review_reference(item),
            str(item.get("character_name") or ""),
            str(item.get("character_code") or ""),
            str(item.get("display_name") or ""),
            *(str(value) for value in aliases),
        }
        if any(
            candidate and candidate.casefold() == lowered
            for candidate in candidates
        ):
            matches.append(item)
    if not matches:
        raise DatabaseNotFoundError(
            "未找到对应的待审核角色，请发送 /团 审核 刷新名单"
        )
    if len(matches) > 1:
        raise ValueError("角色标识不唯一，请改用名单中的审核号")
    return matches[0]


__all__ = [name for name in globals() if not name.startswith('__')]

