from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .registry import _mapping, _public_text, _sequence, number_or_none
from ...contracts.web_views.world_summary import project_world_summary_view
from ...protocol.constants import MODULE_METADATA, WORLD_TAG_PRESETS


_CONTENT_LABELS = {
    "scenes": "场景", "npcs": "常驻角色", "main_quests": "主线任务",
    "side_quests": "支线任务", "factions": "阵营", "endings": "结局",
    "challenge_engine": "遭遇", "clocks": "时钟", "handouts": "线索资料",
    "maps": "地图", "recipes": "配方", "tracks": "成长轨迹",
    "facts": "世界事实",
}


def project_public_world_summary(
    raw: Mapping[str, Any], package: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Merge safe package inventory into the public world summary contract."""

    source = _mapping(raw)
    package_item = _mapping(package)
    modules = _sequence(package_item.get("modules"))
    if package_item.get("version") and not (
        source.get("world_content_version") or source.get("content_version")
    ):
        source["content_version"] = package_item["version"]
    return project_world_summary_view(
        source,
        declared=len(modules) if package_item else None,
        enabled=(
            sum(
                1
                for entry in modules
                if bool(_mapping(entry).get("enabled", True))
            )
            if package_item
            else None
        ),
        include_technical_refs=False,
    )


def world_public_details(view: Mapping[str, Any]) -> dict[str, Any]:
    """Project package facts that are safe and useful in the public detail dialog."""

    item = _mapping(view)
    stats = _mapping(item.get("content_stats"))
    content_summary: list[dict[str, Any]] = []
    for key, label in _CONTENT_LABELS.items():
        count = number_or_none(stats.get(key))
        if count is not None and count >= 0:
            content_summary.append({"label": label, "value": count})

    limits = _mapping(item.get("player_limits"))
    recommended_min = number_or_none(
        limits.get("recommended_min")
        if limits.get("recommended_min") is not None
        else limits.get("minimum_start")
        if limits.get("minimum_start") is not None
        else limits.get("minimum")
    )
    recommended_max = number_or_none(
        limits.get("recommended_max")
        if limits.get("recommended_max") is not None
        else limits.get("maximum")
    )
    maximum = number_or_none(limits.get("maximum"))
    player_summary = ""
    if recommended_min is not None and recommended_max is not None:
        player_summary = f"推荐 {recommended_min:g}—{recommended_max:g} 人"
        if maximum is not None and maximum != recommended_max:
            player_summary += f"，最多 {maximum:g} 人"

    content_version = _public_text(item.get("content_version"), limit=80)
    if content_version.startswith("未提供"):
        content_version = ""
    return {
        "content_version_label": content_version,
        "player_summary": player_summary,
        "content_summary": content_summary[:10],
    }


def world_gameplay_profile(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Expose only the author-declared, player-readable play profile."""

    brief = _mapping(_mapping(raw).get("gameplay_brief"))
    return {
        "tone": _public_text(brief.get("tone"), limit=160),
        "core_loop": _public_text(brief.get("core_loop"), limit=240),
        "recommended_for": _public_text(brief.get("recommended_for"), limit=200),
        "special_rules": [
            value
            for value in (
                _public_text(item, limit=220)
                for item in _sequence(brief.get("special_rules"))[:8]
            )
            if value
        ],
    }


def world_display_tags(raw: Mapping[str, Any]) -> list[dict[str, str]]:
    """Resolve author-selected world-card tags through the plugin vocabulary."""

    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_key in _sequence(_mapping(raw).get("display_tags"))[:4]:
        key = str(raw_key or "").strip()
        label = WORLD_TAG_PRESETS.get(key)
        if not label or key in seen:
            continue
        seen.add(key)
        result.append({"key": key, "label": label})
    return result


def world_declared_capabilities(raw: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Project enabled compiled modules as player-readable gameplay abilities."""

    item = _mapping(raw)
    enabled_modules = [
        str(value or "").strip()
        for value in _sequence(item.get("enabled_modules"))
        if str(value or "").strip()
    ]
    module_contracts = _mapping(item.get("twp_modules"))
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for module_id in enabled_modules or module_contracts.keys():
        key = str(module_id or "").strip()
        if not key or key in seen or key not in MODULE_METADATA:
            continue
        contract = _mapping(module_contracts.get(key))
        if contract and not bool(contract.get("enabled", True)):
            continue
        seen.add(key)
        label, description = MODULE_METADATA[key]
        result.append(
            {
                "key": key,
                "label": label,
                "summary": description,
                "enabled": True,
            }
        )
    return result


def world_resolution_details(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Expose declared, player-readable core gameplay from one compiled revision."""

    item = _mapping(raw)
    elemental = _mapping(item.get("elemental"))
    meanings = _mapping(elemental.get("meanings"))
    elements: list[dict[str, Any]] = []
    for raw_element in _sequence(elemental.get("elements")):
        value = _mapping(raw_element)
        label = _public_text(
            value.get("label") or value.get("name") or raw_element,
            limit=40,
        )
        declared_meaning = _mapping(meanings.get(label))
        meaning = _public_text(
            value.get("meaning") or declared_meaning.get("meaning"), limit=160
        )
        boundary = _public_text(
            value.get("limit") or declared_meaning.get("limit"), limit=160
        )
        if label:
            elements.append({"label": label, "meaning": meaning, "boundary": boundary})

    relation_labels = {
        "counter": "克制", "resist": "抗性", "vulnerable": "易伤",
        "neutralize": "中和", "anchor": "锚定", "bypass": "绕行",
        "amplify": "增幅", "resonance": "共鸣",
    }
    relations: list[dict[str, Any]] = []
    for interaction in _sequence(elemental.get("interactions")):
        value = _mapping(interaction)
        visibility = str(value.get("visibility") or "public").lower()
        if visibility not in {"", "public", "player"}:
            continue
        source = _public_text(value.get("source_element"), limit=40)
        target = _public_text(value.get("target_selector"), limit=60)
        relation_type = str(value.get("relation_type") or "").lower()
        result = relation_labels.get(relation_type, "定向作用")
        summary = _public_text(value.get("public_copy"), limit=180)
        if source and target:
            relations.append({"source": source, "target": target, "result": result, "summary": summary})

    reactions: list[dict[str, Any]] = []
    for reaction in _sequence(elemental.get("reactions")):
        value = _mapping(reaction)
        source = _public_text(value.get("a") or value.get("source"), limit=60)
        target = _public_text(value.get("b") or value.get("target"), limit=60)
        result = _public_text(value.get("result"), limit=60)
        effect = _mapping(value.get("effect"))
        summary = _public_text(effect.get("text") or value.get("summary"), limit=140)
        if source and target and result:
            reactions.append({"source": source, "target": target, "result": result, "summary": summary})

    affinity_labels = {
        "evidence:memory": "记忆类证据",
        "material:consecrated_silver": "祝圣白银",
        "scene:public": "公开场景",
        "structure:crown_node": "灰冠节点",
    }
    affinities: list[dict[str, Any]] = []
    for index, (raw_object, raw_values) in enumerate(_mapping(elemental.get("affinities")).items()):
        values = _mapping(raw_values)
        effects = []
        for element, modifier in values.items():
            number = number_or_none(modifier)
            if number is None or number == 0:
                continue
            effects.append(f"{element}{number:+g}")
        if effects:
            affinities.append({
                "label": affinity_labels.get(str(raw_object), f"公开亲和对象 {index + 1}"),
                "summary": "、".join(effects),
            })

    rules = _mapping(item.get("rules"))
    resolution = _mapping(rules.get("resolution"))
    policy = _mapping(resolution.get("difficulty_policy"))
    difficulty_labels = {
        "safe": "安全", "controlled": "受控", "dangerous": "危险",
        "desperate": "绝境", "lethal": "致命",
    }
    difficulties: list[dict[str, Any]] = []
    for key, label in difficulty_labels.items():
        raw_value = policy.get(key)
        value = number_or_none(raw_value)
        if value is not None:
            difficulties.append({"label": label, "value": value})

    outcome_policy = _mapping(resolution.get("outcome_policy"))
    outcomes: list[dict[str, Any]] = []
    scalar_outcomes = {
        "critical_success_margin": ("大成功", "结果高于难度至少 {value} 点"),
        "cost_success_min_margin": ("代价成功", "结果与难度差至少 {value} 点"),
        "failure_min_margin": ("失败", "结果与难度差低于 {value} 点"),
        "natural_1_critical": ("自然 1", "启用极端失败判定"),
        "natural_20_critical": ("自然 20", "启用极端成功判定"),
    }
    for key, raw_value in outcome_policy.items():
        value = _mapping(raw_value)
        label = _public_text(value.get("label") or value.get("name"), limit=80)
        summary = _public_text(value.get("text") or value.get("summary"), limit=140)
        if not label:
            label = {"critical_success": "大成功", "success": "成功", "partial": "代价成功", "failure": "失败", "critical_failure": "大失败"}.get(str(key), "")
        if not value and str(key) in scalar_outcomes:
            label, template = scalar_outcomes[str(key)]
            if isinstance(raw_value, bool):
                if not raw_value:
                    continue
                summary = template
            else:
                summary = template.format(value=raw_value)
        if label and (summary or value):
            outcomes.append({"label": label, "summary": summary})

    runtime_labels = {
        "scene_graph": "场景与路线", "knowledge_graph": "知识与线索",
        "quest_graph": "任务推进", "time_clock": "时间与倒计时",
        "actor_fate": "伤势与命运", "terminal_conditions": "终局条件",
        "elemental_interactions": "元素交互", "evidence_ledger": "证据账本",
        "accords": "承诺与协定", "assembly": "会盟议程",
        "rumor_network": "传闻网络", "scene_environment": "场景环境",
    }
    runtime_contract = _mapping(item.get("runtime_contract"))
    gameplay_modules = [
        {
            "label": label,
            "state": "已接入运行态",
            "summary": f"提供 {len(_sequence(_mapping(runtime_contract.get(key)).get('capabilities')))} 项读写能力",
        }
        for key, label in runtime_labels.items()
        if key in runtime_contract
    ]
    exposure = _mapping(elemental.get("exposure"))
    exposure_summary = ""
    if exposure:
        maximum = number_or_none(exposure.get("max_layers") or exposure.get("maximum"))
        decay = {
            "scene_end": "场景结束衰减",
            "round_end": "回合结束衰减",
        }.get(str(exposure.get("decay") or ""), "按世界规则衰减")
        exposure_summary = f"最多 {maximum:g} 层，{decay}" if maximum is not None else decay

    coverage_checks = {
        "难度阶梯": bool(difficulties),
        "难度与结果": bool(outcomes),
        "运行玩法": bool(gameplay_modules or runtime_contract),
        "元素语义": bool(elements) if elemental else True,
        "定向作用": bool(relations) if elemental else True,
        "元素反应": bool(reactions) if elemental else True,
        "对象亲和": bool(affinities) if elemental else True,
        "暴露与衰减": bool(exposure_summary) if elemental else True,
    }
    covered = sum(1 for state in coverage_checks.values() if state)
    total = len(coverage_checks)
    return {
        "relations": relations[:16],
        "reactions": reactions[:20],
        "elements": elements[:12],
        "affinities": affinities[:12],
        "gameplay_modules": gameplay_modules,
        "exposure_summary": exposure_summary,
        "difficulties": difficulties,
        "outcomes": outcomes[:8],
        "default_difficulty": number_or_none(rules.get("default_difficulty")),
        "minimum_difficulty": number_or_none(rules.get("difficulty_min")),
        "maximum_difficulty": number_or_none(rules.get("difficulty_max")),
        "elemental_enabled": bool(elemental),
        "coverage": {
            "covered": covered,
            "total": total,
            "percent": round(covered * 100 / total),
            "gaps": [label for label, state in coverage_checks.items() if not state],
        },
    }


__all__ = [
    "project_public_world_summary",
    "world_declared_capabilities",
    "world_display_tags",
    "world_gameplay_profile",
    "world_public_details",
    "world_resolution_details",
]
