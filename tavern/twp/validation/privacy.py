from .common import *
from .structure import *
from .references import *
from .content import *

def _walk_tendency_signals(value: Any, path: str = "rules") -> list[tuple[str, Mapping[str, Any]]]:
    found: list[tuple[str, Mapping[str, Any]]] = []
    if isinstance(value, Mapping):
        raw_signals = value.get("tendency_signals")
        if isinstance(raw_signals, Sequence) and not isinstance(
            raw_signals, (str, bytes)
        ):
            for index, signal in enumerate(raw_signals):
                if isinstance(signal, Mapping):
                    found.append((f"{path}.tendency_signals[{index}]", signal))
        for key, item in value.items():
            if key != "tendency_signals":
                found.extend(_walk_tendency_signals(item, f"{path}.{key}"))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, item in enumerate(value):
            found.extend(_walk_tendency_signals(item, f"{path}[{index}]"))
    return found


def _condition_mentions_secret(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            text = f"{key}:{item}".casefold()
            if any(
                marker in text
                for marker in (
                    "secret",
                    "private",
                    "dm_only",
                    "hidden_fact",
                    "visibility:host",
                )
            ):
                return True
            if _condition_mentions_secret(item):
                return True
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return any(_condition_mentions_secret(item) for item in value)
    return False


def check_tendency_signals(world: Mapping[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    rules = _mapping(world.get("rules"))
    localized = set()
    catalogs = world.get("resolved_text_catalog")
    if isinstance(catalogs, Mapping):
        for catalog in catalogs.values():
            if isinstance(catalog, Mapping):
                localized.update(str(key) for key in catalog)
    for path, signal in _walk_tendency_signals(rules):
        dimension = str(signal.get("dimension") or "")
        direction = signal.get("direction")
        weight = signal.get("weight")
        rationale = str(signal.get("rationale_text_id") or "")
        conditions = signal.get("conditions") or {}
        invalid = (
            dimension not in TENDENCY_DIMENSIONS
            or direction not in {-1, 1}
            or isinstance(weight, bool)
            or not isinstance(weight, int)
            or not 1 <= weight <= 3
            or not rationale
            or (localized and rationale not in localized)
            or bool(validate_condition_tree(conditions))
        )
        if invalid:
            issues.append(
                _issue(
                    "error",
                    "tendency.signal.invalid",
                    "倾向信号的维度、方向、权重、本地化说明或条件不合法。",
                    path,
                    "维度使用固定六项，direction 为 -1/1，weight 为 1-3，并提供可解析的公开说明。",
                )
            )
        if _condition_mentions_secret(conditions):
            issues.append(
                _issue(
                    "error",
                    "tendency.signal.secret_dependency",
                    "倾向信号依赖玩家不可见的秘密或私聊事实。",
                    path,
                    "只使用玩家已知且已经提交的结构化状态作为归因条件。",
                )
            )
    return issues


def check_template(world: Mapping[str, Any]) -> dict[str, Any]:
    """统一模板体检入口。返回 {compatible, errors, warnings, suggestions, summary, matrix}。"""
    try:
        template = card_template(world)
    except Exception as exc:  # noqa: BLE001
        return {
            "compatible": False,
            "errors": [_issue("error", "template.unreadable", f"无法读取角色模板：{exc}")],
            "warnings": [],
            "suggestions": [],
            "summary": {"free_fields": 0, "single": 0, "multi": 0, "profession_coverage": "0/0"},
            "matrix": [],
        }
    template = dict(template)
    template["_world"] = world
    rules = world.get("rules") if isinstance(world, Mapping) else {}
    template["_world_rules"] = rules if isinstance(rules, Mapping) else {}
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    suggestions: list[dict[str, Any]] = []

    groups = [
        check_references(template),
        check_dependency_cycles(template),
        check_archetype_packs(template),
        check_private_leak(template),
        check_preset_lifecycle(world),
        check_preset_libraries(world),
        check_award_references(world),
        check_event_cascades(world),
        check_profession_content(world, template),
        check_tendency_signals(world),
    ]
    if str(world.get("slug") or "") == "thirteenth-seat-new-era":
        groups.append(check_belief_content(template))
    if _uses_strict_creation_contract(template):
        groups[0:0] = [
            check_free_fields(template),
            check_fixed_counts(template),
        ]

    for group in groups:
        for issue in group:
            if issue["level"] == "error":
                errors.append(issue)
            elif issue["level"] == "warning":
                warnings.append(issue)
            else:
                suggestions.append(issue)

    coverage = check_profession_coverage(template)
    if _uses_strict_creation_contract(template):
        for row in coverage["matrix"]:
            missing: list[str] = []
            if row["specializations"] < MIN_SPECIALIZATIONS:
                missing.append(f"专精 {row['specializations']}/{MIN_SPECIALIZATIONS}")
            if row["weapons"] < MIN_WEAPONS:
                missing.append(f"武器 {row['weapons']}/{MIN_WEAPONS}")
            if row["armors"] < MIN_ARMORS:
                missing.append(f"防具 {row['armors']}/{MIN_ARMORS}")
            if row["abilities"] < FIXED_MULTI_MIN_CANDIDATES["abilities"]:
                missing.append(f"能力 {row['abilities']}/{FIXED_MULTI_MIN_CANDIDATES['abilities']}")
            if row["feats"] < FIXED_MULTI_MIN_CANDIDATES["specialties"]:
                missing.append(f"专长 {row['feats']}/{FIXED_MULTI_MIN_CANDIDATES['specialties']}")
            if row["weaknesses"] < FIXED_MULTI_MIN_CANDIDATES["weakness"]:
                missing.append(f"弱点 {row['weaknesses']}/{FIXED_MULTI_MIN_CANDIDATES['weakness']}")
            if missing:
                row["status"] = "blocked"
                row["missing"] = missing
                errors.append(
                    _issue(
                        "error",
                        "coverage.below_minimum",
                        f"职业「{row['profession']}」候选不足：" + "、".join(missing),
                        f"profession_presets.{row['profession']}",
                        "低于最低数量是阻断错误，不是警告",
                    )
                )

    flow = template.get("creation_flow")
    flow = flow if isinstance(flow, Mapping) else {}
    modes = _sequence(flow.get("modes"))
    packs = _sequence(flow.get("archetype_packs"))
    single_count = len(_single_fields(template))
    multi_count = len(_multi_fields(template))
    free_count = len(
        [
            item
            for item in _sequence(template.get("fields"))
            if isinstance(item, Mapping)
            and str(item.get("type") or "") in {"text", "textarea"}
        ]
    )
    passed = len([row for row in coverage["matrix"] if row["status"] == "ok"])
    total = max(1, len(coverage["matrix"]))
    library_catalog = preset_library_catalog(world)
    summary = {
        "free_fields": free_count,
        "single": single_count,
        "multi": multi_count,
        "profession_coverage": f"{passed}/{total}",
        "modes": len(modes),
        "archetype_packs": len(packs),
        "version": str(template.get("version") or ""),
        "preset_libraries": len(library_catalog.get("items", [])),
        "preset_library_metadata_complete": bool(
            library_catalog.get("metadata_complete")
        ),
    }
    return {
        "compatible": not errors,
        "errors": errors,
        "warnings": warnings,
        "suggestions": suggestions,
        "summary": summary,
        "matrix": coverage["matrix"],
    }


def coverage_matrix(world: Mapping[str, Any]) -> dict[str, Any]:
    """覆盖矩阵（§27.4/§26.1 标签四）。"""
    template = card_template(world)
    return check_profession_coverage(template)


__all__ = [name for name in globals() if not name.startswith('__')]

