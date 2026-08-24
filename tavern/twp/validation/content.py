from .common import *
from .structure import *
from .references import *

def check_profession_content(
    world: Mapping[str, Any],
    template: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Run release-blocking profession content checks."""
    issues: list[dict[str, Any]] = []
    professions = [
        item
        for item in _sequence(template.get("profession_presets"))
        if isinstance(item, Mapping)
    ]
    by_slug = {
        _candidate_slug(item.get("id"), "prof:"): item
        for item in professions
    }
    try:
        registry = EntityRegistry(world)
    except Exception as exc:  # noqa: BLE001
        return [
            _issue(
                "error",
                "actor.profession.bundle_incomplete",
                f"职业内容无法建立运行引用目录：{exc}",
                "rules.actor.profession_presets",
                "先修复世界实体索引，再检查职业专属内容。",
            )
        ]

    effect_owners: dict[str, list[str]] = {}
    for slug, profession in sorted(by_slug.items()):
        specializations = [
            item
            for item in _sequence(profession.get("specialization_options"))
            if isinstance(item, Mapping)
        ]
        for index, specialization in enumerate(specializations):
            label = _text(
                specialization.get("label")
                or specialization.get("value")
                or specialization.get("name"),
                120,
            )
            specialization_label = (
                f"专精〈{label}〉" if label else "缺少名称的专精"
            )
            path = (
                f"rules.actor.profession_presets.{slug}."
                f"specialization_options[{index}]"
            )
            if NUMBERED_LABEL_PATTERN.search(label):
                issues.append(
                    _issue(
                        "error",
                        "actor.specialization.numbered_label",
                        f"专精〈{label}〉仍使用编号式占位名称。",
                        path,
                        "保留稳定引用，但替换玩家可见名称、说明、场景与限制。",
                        entity=label,
                    )
                )
            summary = _text(specialization.get("summary"), 400)
            description = _text(specialization.get("description"), 500)
            advantages = _sequence(specialization.get("advantages"))
            limitations = _sequence(specialization.get("limitations"))
            story_hooks = _sequence(specialization.get("story_hooks"))
            advantage_copy = [
                _text(item.get("description"), 400)
                for item in advantages
                if isinstance(item, Mapping)
            ]
            limitation_copy = [
                _text(item.get("description"), 400)
                for item in limitations
                if isinstance(item, Mapping)
            ]
            if (
                len(summary) < 20
                or len(description) < 20
                or not advantage_copy
                or any(len(item) < 20 for item in advantage_copy)
                or not limitation_copy
                or any(len(item) < 8 for item in limitation_copy)
                or not story_hooks
            ):
                issues.append(
                    _issue(
                        "error",
                        "actor.specialization.copy_incomplete",
                        f"{specialization_label}缺少完整说明、优势、限制或剧情钩子。",
                        path,
                        "补齐 summary、description、advantages、limitations 和 story_hooks。",
                        entity=label,
                    )
                )
            signature = candidate_rule_apply_signature(specialization)
            effect_owners.setdefault(signature, []).append(
                specialization_label
            )

        if slug not in OWNED_PROFESSIONS:
            continue
        owned_abilities = [
            item
            for item in _sequence(profession.get("ability_options"))
            if isinstance(item, Mapping)
            and _candidate_slug(item.get("id"), "ability:") == slug
        ]
        owned_weapons = [
            item
            for item in _sequence(profession.get("starting_weapon_options"))
            if isinstance(item, Mapping)
            and _candidate_slug(item.get("id"), "weapon:") == slug
        ]
        owned_armors = [
            item
            for item in _sequence(profession.get("starting_armor_options"))
            if isinstance(item, Mapping)
            and _candidate_slug(item.get("id"), "armor:") == slug
        ]
        owned_weaknesses = [
            item
            for item in _sequence(profession.get("weakness_options"))
            if isinstance(item, Mapping)
            and _candidate_slug(item.get("id"), "weakness:") == slug
        ]
        counts = (
            ("actor.profession.owned_ability_insufficient", "能力", len(owned_abilities), 4),
            ("actor.profession.owned_equipment_insufficient", "武器", len(owned_weapons), 2),
            ("actor.profession.owned_equipment_insufficient", "防具", len(owned_armors), 2),
            ("actor.profession.owned_weakness_insufficient", "弱点", len(owned_weaknesses), 2),
        )
        for code, label, actual, required in counts:
            if actual < required:
                issues.append(
                    _issue(
                        "error",
                        code,
                        f"职业「{profession.get('name') or slug}」专属{label}不足：{actual}/{required}。",
                        f"rules.actor.profession_presets.{slug}",
                        "补充使用该职业稳定命名空间的独立内容。",
                        entity=str(profession.get("name") or slug),
                    )
                )
        if any(actual < required for _, _, actual, required in counts):
            issues.append(
                _issue(
                    "error",
                    "actor.profession.owned_bundle_insufficient",
                    f"职业「{profession.get('name') or slug}」的专属内容包不完整。",
                    f"rules.actor.profession_presets.{slug}",
                    "能力、武器、防具与弱点必须同时达到最低数量。",
                    entity=str(profession.get("name") or slug),
                )
            )
        source_tags = {
            str(item.get("id") or "")
            for item in _sequence(profession.get("ability_options"))
            if isinstance(item, Mapping)
            and not str(item.get("id") or "").startswith(f"ability:{slug}.")
        }
        base_candidates = [
            set(
                str(item.get("id") or "")
                for item in _sequence(candidate.get("ability_options"))
                if isinstance(item, Mapping)
            )
            for base_slug, candidate in by_slug.items()
            if base_slug not in OWNED_PROFESSIONS
        ]
        similarity = max(
            (
                len(source_tags & refs) / max(1, len(source_tags | refs))
                for refs in base_candidates
            ),
            default=0.0,
        )
        if similarity > 0.75:
            issues.append(
                _issue(
                    "error",
                    "actor.profession.bundle_too_similar",
                    f"职业「{profession.get('name') or slug}」与既有职业的能力候选过于相似（{similarity:.2f}）。",
                    f"rules.actor.profession_presets.{slug}.ability_options",
                    "减少整包复用，保留少量交叉训练并增加独立可执行能力。",
                    entity=str(profession.get("name") or slug),
                )
            )
        for index, ability in enumerate(owned_abilities):
            label = _text(ability.get("label") or ability.get("value"), 120)
            ability_label = f"能力〈{label}〉" if label else "缺少名称的能力"
            issue = _operation_issue(
                registry,
                ability,
                path=(
                    f"rules.actor.profession_presets.{slug}."
                    f"ability_options[{index}]"
                ),
                entity=label,
            )
            if issue:
                issues.append(issue)
            signature = json.dumps(
                {
                    "operations": ability.get("operations") or [],
                    "conditions": ability.get("conditions") or [],
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            effect_owners.setdefault(signature, []).append(
                ability_label
            )

    for owners in effect_owners.values():
        if len(owners) < 2:
            continue
        # Empty/no-op signatures have their own precise issue code above.
        if owners[0].startswith("能力") and '"operations":[]' in owners[0]:
            continue
        issues.append(
            _issue(
                "error",
                "actor.copy.mechanical_duplicate",
                "以下玩家候选具有完全相同的机械声明：" + "、".join(owners[:6]),
                "rules.actor.profession_presets",
                "保留共享能力时明确标为交叉训练；专属内容必须使用不同输入、效果、代价或边界。",
            )
        )
    return issues


def check_belief_content(
    template: Mapping[str, Any],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    preset_sets = _mapping(template.get("preset_sets"))
    beliefs = [
        item
        for item in _sequence(preset_sets.get("belief_presets"))
        if isinstance(item, Mapping)
    ]
    expected_ids = {
        "belief:truth_free",
        "belief:freedom_first",
        "belief:protect_weak",
        "belief:preserve_knowledge",
        "belief:oath_above_law",
        "belief:law_above_whim",
        "belief:power_has_cost",
        "belief:memory_common",
        "belief:second_chance",
        "belief:nature_boundary",
    }
    actual_ids = [str(item.get("id") or "") for item in beliefs]
    if len(beliefs) != 10 or set(actual_ids) != expected_ids:
        return [
            _issue(
                "error",
                "actor.belief.catalog_invalid",
                "核心信念作者源必须恰好包含产品规定的十项。",
                "rules.actor.preset_sets.belief_presets",
                "删除第十一项、补回缺项，并迁移全部旧信念引用。",
            )
        ]
    belief_field = next(
        (
            item
            for item in _sequence(template.get("fields"))
            if isinstance(item, Mapping)
            and str(item.get("key") or "") == "belief"
        ),
        {},
    )
    if int(belief_field.get("page_size") or 0) != 10:
        issues.append(
            _issue(
                "error",
                "actor.belief.page_size_invalid",
                "核心信念必须在同一逻辑页展示完整十项。",
                "rules.actor.fields.belief.page_size",
            )
        )

    known_refs: set[str] = set()

    def collect_ids(value: Any) -> None:
        if isinstance(value, Mapping):
            identifier = str(value.get("id") or "").strip()
            if identifier:
                known_refs.add(identifier)
            for child in value.values():
                collect_ids(child)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for child in value:
                collect_ids(child)

    collect_ids(template)
    profession_coverage: set[str] = set()
    species_coverage: set[str] = set()
    affinity_keys = (
        "profession_refs",
        "specialization_refs",
        "species_refs",
        "culture_refs",
        "origin_refs",
        "hometown_refs",
        "social_identity_refs",
        "faction_refs",
    )
    for index, belief in enumerate(beliefs):
        path = f"rules.actor.preset_sets.belief_presets[{index}]"
        label = _text(belief.get("label") or belief.get("value"), 120)
        belief_label = (
            f"核心信念〈{label}〉" if label else "缺少名称的核心信念"
        )
        affinity = belief.get("affinity")
        if (
            len(_text(belief.get("summary"), 400)) < 20
            or len(_text(belief.get("description"), 500)) < 20
            or not _sequence(belief.get("advantages"))
            or not _sequence(belief.get("limitations"))
            or not _sequence(belief.get("story_hooks"))
            or not _text(belief.get("text_id"), 240)
            or not isinstance(affinity, Mapping)
        ):
            issues.append(
                _issue(
                    "error",
                    "actor.belief.copy_incomplete",
                    f"{belief_label}内容或关联不完整。",
                    path,
                )
            )
            continue
        for key in affinity_keys:
            refs = [str(item) for item in _sequence(affinity.get(key))]
            if not refs:
                issues.append(
                    _issue(
                        "error",
                        "actor.belief.affinity_incomplete",
                        f"核心信念〈{label}〉缺少 {key} 关联。",
                        f"{path}.affinity.{key}",
                    )
                )
            for ref in refs:
                if ref not in known_refs:
                    issues.append(
                        _issue(
                            "error",
                            "actor.belief.affinity_ref_missing",
                            f"核心信念〈{label}〉引用不存在：{ref}",
                            f"{path}.affinity.{key}",
                        )
                    )
            if key == "profession_refs":
                profession_coverage.update(refs)
            elif key == "species_refs":
                species_coverage.update(refs)
        rules = [
            item
            for item in _sequence(affinity.get("synergy_rules"))
            if isinstance(item, Mapping)
        ]
        if len(rules) < 3:
            issues.append(
                _issue(
                    "error",
                    "actor.belief.synergy_incomplete",
                    f"核心信念〈{label}〉至少需要三条跨维关联规则。",
                    f"{path}.affinity.synergy_rules",
                )
            )
        for rule_index, rule in enumerate(rules):
            requirements = [
                item
                for item in _sequence(rule.get("requires_all"))
                if isinstance(item, Mapping)
            ]
            if (
                len(requirements) < 2
                or not _text(rule.get("id"), 160)
                or len(_text(rule.get("why"), 500)) < 20
            ):
                issues.append(
                    _issue(
                        "error",
                        "actor.belief.synergy_incomplete",
                        f"核心信念〈{label}〉的组合关联规则不完整。",
                        f"{path}.affinity.synergy_rules[{rule_index}]",
                    )
                )
            for requirement in requirements:
                for ref in _sequence(requirement.get("values")):
                    if str(ref) not in known_refs:
                        issues.append(
                            _issue(
                                "error",
                                "actor.belief.affinity_ref_missing",
                                f"核心信念〈{label}〉组合规则引用不存在：{ref}",
                                f"{path}.affinity.synergy_rules[{rule_index}]",
                            )
                        )
    expected_professions = {
        str(item.get("id") or "")
        for item in _sequence(template.get("profession_presets"))
        if isinstance(item, Mapping)
    }
    expected_species = {
        str(item.get("id") or "")
        for item in _sequence(preset_sets.get("species_presets"))
        if isinstance(item, Mapping)
    }
    for label, missing in (
        ("职业", expected_professions - profession_coverage),
        ("种族", expected_species - species_coverage),
    ):
        if missing:
            issues.append(
                _issue(
                    "error",
                    "actor.belief.affinity_coverage_incomplete",
                    f"核心信念关联矩阵未覆盖全部{label}："
                    + "、".join(sorted(missing)),
                    "rules.actor.preset_sets.belief_presets",
                )
            )
    return issues


__all__ = [name for name in globals() if not name.startswith('__')]

