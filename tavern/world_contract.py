from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .presets import validate_preset_dimensions
from .entity_registry import EntityRegistry, module_value
from .event_pipeline import EVENT_PHASES, STACKING_STRATEGIES
from .operation_engine import OPERATION_TYPES, PERSISTENCE_SCOPES
from .operation_engine import OperationEngine
from .capability_service import CapabilityService
from .chat_experience import validate_chat_experience
from .constants import PLUGIN_VERSION
from .protocol.constants import (
    TWP_CORE_VERSION,
    TWP_MODULE_API_VERSION,
    TWP_VERSION,
    WORLD_TAG_PRESETS,
)
from .contracts.actor_fate import (
    parse_actor_fate,
    parse_terminal_conditions,
    validate_actor_fate,
    validate_terminal_conditions,
)


WORLD_SCHEMA_VERSION = 12
# 仅用于当前 TWP 编译世界模型的内部校验，不是公开世界协议版本。
SUPPORTED_WORLD_SCHEMA_VERSIONS = frozenset({WORLD_SCHEMA_VERSION})
FEATURE_VERSIONS = {
    # 原子切换全部插件自有模块与运行契约，避免两段式
    # 1.0/2.0 在作者源、编译物、运行验证与消费者之间继续漂移。
    feature: TWP_MODULE_API_VERSION
    for feature in (
        "actor",
        "time_clock",
        "relationship_graph",
        "items_inventory",
        "economy",
        "capability_effects",
        "ending",
        "entity_registry",
        "condition_engine",
        "operation_engine",
        "event_pipeline",
        "resolution_receipt",
        "capabilities",
        "resources",
        "runtime_effects",
        "objects",
        "resolution_methods",
        "interaction_rules",
        "action_intents",
        "chat_experience",
        "scene_graph",
        "quest_graph",
        "knowledge_graph",
        "npc_lifecycle",
        "faction_state",
        "human_dm",
        "challenge_engine",
        "tactical_conflict",
        "actor_fate",
        "terminal_conditions",
        "progression",
        "crafting",
        "localization",
        "maps_handouts",
        "distribution",
        "simulation",
        "adaptive_ui",
    )
}
STAT_MODES = {"none", "manual", "preset", "preset_stack"}
RESOLUTION_MODES = {"none", "narrative", "dice_only", "attribute"}

DEFAULT_DANGER_LEVELS = (
    {"id": "safe", "label": "安全"},
    {"id": "controlled", "label": "可控"},
    {"id": "dangerous", "label": "危险"},
    {"id": "desperate", "label": "绝境"},
    {
        "id": "lethal",
        "label": "致命",
        "requires_visible_consequence": True,
    },
)

DEFAULT_DIFFICULTY_POLICY = {
    "safe": None,
    "controlled": 9,
    "dangerous": 13,
    "desperate": 17,
    "lethal": 21,
}

DEFAULT_OUTCOME_POLICY = {
    "natural_20_critical": True,
    "natural_1_critical": True,
    "critical_success_margin": 10,
    "cost_success_min_margin": -4,
    "failure_min_margin": -9,
}


def _version_tuple(value: Any) -> tuple[int, int, int] | None:
    text = str(value or "").strip().lower().removeprefix("v")
    numeric = text.split("-", 1)[0]
    parts = numeric.split(".")
    if not 1 <= len(parts) <= 3 or any(not part.isdigit() for part in parts):
        return None
    return tuple((int(parts[index]) if index < len(parts) else 0) for index in range(3))


def _feature_requirement(value: object) -> tuple[str, tuple[int, int, int]]:
    text = str(value or "").strip()
    if "@>=" not in text:
        raise ValueError(f"required_features 格式无效：{text}")
    name, raw_version = text.split("@>=", 1)
    parsed = _version_tuple(raw_version)
    if not name or parsed is None:
        raise ValueError(f"required_features 格式无效：{text}")
    return name, parsed


def stats_mode(stats: Mapping[str, Any] | None) -> str:
    stats = stats if isinstance(stats, Mapping) else {}
    mode = str(stats.get("mode") or "").strip().lower()
    if mode in STAT_MODES:
        return mode
    # C6: an omitted stats contract means that this world has no standard
    # attribute-allocation phase.  The platform must not invent attributes.
    return "none"


def world_contract(world: Mapping[str, Any] | None) -> dict[str, Any]:
    """Build the runtime contract for the current world protocol."""

    source = world if isinstance(world, Mapping) else {}
    rules = source.get("rules")
    rules = rules if isinstance(rules, Mapping) else source
    card = rules.get("actor")
    card = card if isinstance(card, Mapping) else {}
    stats = card.get("stats")
    stats = stats if isinstance(stats, Mapping) else {}
    generation = card.get("stat_generation")
    generation = generation if isinstance(generation, Mapping) else {}
    if str(generation.get("mode") or "").lower() == "preset_stack":
        stats = {
            **dict(stats),
            "mode": "preset_stack",
            "stat_generation": dict(generation),
        }
    try:
        version = int(
            source.get(
                "internal_world_model_revision",
                rules.get(
                    "internal_world_model_revision",
                    WORLD_SCHEMA_VERSION
                    if str(
                        (
                            source.get("protocol")
                            if isinstance(source.get("protocol"), Mapping)
                            else {}
                        ).get("name")
                        or ""
                    ).casefold()
                    == "twp"
                    else 0,
                ),
            )
        )
    except (TypeError, ValueError):
        version = 0

    protocol = module_value(source, "protocol", {})
    protocol = protocol if isinstance(protocol, Mapping) else {}
    features = protocol.get("features", {})
    features = (
        {str(key): str(value) for key, value in features.items()}
        if isinstance(features, Mapping)
        else {}
    )
    required_features = module_value(source, "required_features", [])
    if not isinstance(required_features, Sequence) or isinstance(required_features, (str, bytes)):
        required_features = []

    resolution = rules.get("resolution")
    if isinstance(resolution, Mapping):
        resolution_raw = dict(resolution)
        resolution_mode = str(
            resolution_raw.get("mode") or "none"
        ).lower()
    else:
        resolution_raw = {"dice_system": str(resolution or "d20")}
        resolution_mode = (
            "attribute" if stats_mode(stats) != "none" else "dice_only"
        )
    if resolution_mode not in RESOLUTION_MODES:
        resolution_mode = "none"

    attributes = [
        dict(item)
        for item in stats.get("attributes", [])
        if isinstance(item, Mapping) and item.get("key")
    ]
    allowed = resolution_raw.get("allowed_attributes")
    if not isinstance(allowed, Sequence) or isinstance(
        allowed, (str, bytes)
    ):
        allowed = [str(item["key"]) for item in attributes]
    danger_levels = rules.get("danger_levels")
    if not isinstance(danger_levels, Sequence) or isinstance(
        danger_levels, (str, bytes)
    ):
        danger_levels = DEFAULT_DANGER_LEVELS
    presentation = rules.get("option_presentation")
    presentation = (
        presentation if isinstance(presentation, Mapping) else {}
    )
    annotation = presentation.get("annotation")
    annotation = annotation if isinstance(annotation, Mapping) else {}
    difficulty_policy = dict(DEFAULT_DIFFICULTY_POLICY)
    if isinstance(resolution_raw.get("difficulty_policy"), Mapping):
        difficulty_policy.update(
            dict(resolution_raw.get("difficulty_policy") or {})
        )
    outcome_policy = dict(DEFAULT_OUTCOME_POLICY)
    if isinstance(resolution_raw.get("outcome_policy"), Mapping):
        outcome_policy.update(dict(resolution_raw.get("outcome_policy") or {}))
    return {
        "version": version,
        "minimum_plugin_version": str(
            source.get("minimum_plugin_version") or ""
        ),
        "protocol": {
            "name": str(protocol.get("name") or ""),
            "version": str(protocol.get("version") or ""),
            "core": str(protocol.get("core") or ""),
            "compiler_abi": str(protocol.get("compiler_abi") or ""),
            "features": features,
        },
        "required_features": [str(item) for item in required_features],
        "stats": {**dict(stats), "mode": stats_mode(stats)},
        "attributes": attributes,
        "resolution": {
            "mode": resolution_mode,
            "dice_system": str(
                resolution_raw.get("dice_system") or "d20"
            ),
            "unknown_attribute": str(
                resolution_raw.get("unknown_attribute") or "reject"
            ),
            "generic_check": dict(
                resolution_raw.get("generic_check") or {}
            ),
            "allowed_attributes": [str(item) for item in allowed],
            "difficulty_policy": difficulty_policy,
            "outcome_policy": outcome_policy,
        },
        "danger_levels": [
            dict(item)
            for item in danger_levels
            if isinstance(item, Mapping)
        ],
        "annotation": {
            "enabled": bool(annotation.get("enabled", True)),
            "show_danger": bool(annotation.get("show_danger", True)),
            "show_check_attribute": bool(
                annotation.get("show_check_attribute", True)
            ),
            "show_failure_consequence": bool(
                annotation.get("show_failure_consequence", False)
            ),
        },
        "capabilities": dict(
            source.get("capabilities")
            or rules.get("capabilities")
            or {}
        ),
        "economy": _economy_contract(rules),
        "preset_dimensions": list(card.get("preset_dimensions") or []),
        "knowledge_boundary": dict(
            rules.get("knowledge_boundary") or {}
        ),
        "content_boundary": dict(rules.get("content_boundary") or {}),
        "chat_experience": validate_chat_experience(source),
    }


def _economy_contract(rules: Mapping[str, Any]) -> dict[str, Any]:
    """Parse the C5 multi-shop economy contract.

    ``shops`` is the only definition source.  Callers must select a concrete
    shop and consume its runtime MarketView instead of assuming one global
    static shop.
    """
    raw = rules.get("economy")
    if not isinstance(raw, Mapping):
        return {"available": False, "policy": {}, "shops": []}
    currencies = raw.get("currencies") or []
    if not isinstance(currencies, list):
        currencies = []
    initial_wallets = raw.get("initial_wallets") or []
    if not isinstance(initial_wallets, list):
        initial_wallets = []
    exchange_rules = raw.get("exchange_rules") or []
    if not isinstance(exchange_rules, list):
        exchange_rules = []
    policy = raw.get("policy")
    if not isinstance(policy, Mapping):
        policy = {}
    shops = raw.get("shops") or []
    if not isinstance(shops, list):
        shops = []
    normalized_shops: list[dict[str, Any]] = []
    seen_shop_ids: set[str] = set()
    for raw_shop in shops:
        if not isinstance(raw_shop, Mapping):
            continue
        shop_id = str(raw_shop.get("shop_id") or "").strip()
        if not shop_id:
            raise ValueError("rules.economy.shops 每项必须声明 shop_id")
        if shop_id in seen_shop_ids:
            raise ValueError(f"商店 ID 重复：{shop_id}")
        seen_shop_ids.add(shop_id)
        offers = raw_shop.get("offers") or []
        if not isinstance(offers, list):
            raise ValueError(f"商店 {shop_id} 的 offers 必须是数组")
        normalized_offers: list[dict[str, Any]] = []
        seen_offer_ids: set[str] = set()
        for raw_offer in offers:
            if not isinstance(raw_offer, Mapping):
                continue
            offer_id = str(raw_offer.get("offer_id") or "").strip()
            item_ref = str(raw_offer.get("item_ref") or "").strip()
            if not offer_id or not item_ref:
                raise ValueError(f"商店 {shop_id} 的商品必须声明 offer_id 和 item_ref")
            if offer_id in seen_offer_ids:
                raise ValueError(f"商店 {shop_id} 的商品 ID 重复：{offer_id}")
            seen_offer_ids.add(offer_id)
            normalized_offers.append(dict(raw_offer))
        normalized_shops.append(
            {
                **dict(raw_shop),
                "shop_id": shop_id,
                "label": str(raw_shop.get("label") or raw_shop.get("name") or shop_id),
                "region_refs": [
                    str(item) for item in raw_shop.get("region_refs", [])
                ]
                if isinstance(raw_shop.get("region_refs"), list)
                else [],
                "scene_refs": [
                    str(item) for item in raw_shop.get("scene_refs", [])
                ]
                if isinstance(raw_shop.get("scene_refs"), list)
                else [],
                "availability_conditions": dict(
                    raw_shop.get("availability_conditions") or {}
                )
                if isinstance(raw_shop.get("availability_conditions"), Mapping)
                else {},
                "restock_policy": dict(raw_shop.get("restock_policy") or {})
                if isinstance(raw_shop.get("restock_policy"), Mapping)
                else {},
                "offers": normalized_offers,
            }
        )
    return {
        "available": True,
        "currencies": [dict(item) for item in currencies if isinstance(item, Mapping)],
        "initial_wallets": [
            dict(item) for item in initial_wallets if isinstance(item, Mapping)
        ],
        "exchange_rules": [
            dict(item) for item in exchange_rules if isinstance(item, Mapping)
        ],
        "policy": dict(policy),
        "shops": normalized_shops,
    }


def starter_loadout(world: Mapping[str, Any]) -> dict[str, Any]:
    """每玩家开局物资（1.0.0-A7）：rules.starter_loadout.items = {物品: 数量}。"""
    rules = world.get("rules")
    if not isinstance(rules, Mapping):
        return {}
    raw = rules.get("starter_loadout")
    if not isinstance(raw, Mapping):
        return {}
    items = raw.get("items")
    if not isinstance(items, Mapping):
        return {}
    return {
        "label": str(raw.get("label") or "开局物资"),
        "items": {
            str(name): max(1, int(count))
            for name, count in items.items()
            if str(name).strip() and isinstance(count, (int, float)) and int(count) > 0
        },
    }


def shop_offers(
    world: Mapping[str, Any],
    shop_ref: str,
) -> list[dict[str, Any]]:
    """Return offers for one explicitly selected C5 shop."""

    contract = world_contract(world)
    shops = contract.get("economy", {}).get("shops", [])
    for shop in shops if isinstance(shops, list) else []:
        if (
            isinstance(shop, Mapping)
            and str(shop.get("shop_id") or "") == str(shop_ref or "")
        ):
            return [
                dict(item)
                for item in shop.get("offers", [])
                if isinstance(item, Mapping)
            ]
    return []


def _declared_module_contracts(
    world: Mapping[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Return package-owned module descriptors and runtime contracts.

    C5 deliberately treats module ids as world-package data.  The plugin only
    owns the shape of the descriptor/runtime contract; an extension therefore
    does not need to be added to a core allow-list before it can be validated.
    """

    descriptors: dict[str, dict[str, Any]] = {}
    raw_modules = world.get("twp_modules")
    if isinstance(raw_modules, Sequence) and not isinstance(
        raw_modules, (str, bytes)
    ):
        for raw in raw_modules:
            if not isinstance(raw, Mapping):
                raise ValueError("twp_modules 的每一项都必须是对象")
            module_id = str(raw.get("module_id") or raw.get("id") or "").strip()
            if not module_id:
                raise ValueError("twp_modules 模块缺少 module_id")
            if module_id in descriptors:
                raise ValueError(f"twp_modules 模块重复：{module_id}")
            descriptors[module_id] = dict(raw)

    runtime_contracts: dict[str, dict[str, Any]] = {}
    raw_runtime = world.get("runtime_contract")
    if raw_runtime is not None:
        if not isinstance(raw_runtime, Mapping):
            raise ValueError("runtime_contract 必须是对象")
        for module_id, raw in raw_runtime.items():
            module_key = str(module_id).strip()
            if not module_key or not isinstance(raw, Mapping):
                raise ValueError("runtime_contract 每个模块都必须是具名对象")
            runtime_contracts[module_key] = dict(raw)
    return descriptors, runtime_contracts


def _validate_declared_modules(
    world: Mapping[str, Any],
    declared_features: Mapping[str, str],
) -> dict[str, str]:
    descriptors, runtime_contracts = _declared_module_contracts(world)
    dynamic_versions: dict[str, str] = {}
    if not descriptors and not runtime_contracts:
        return dynamic_versions

    unknown_runtime = sorted(set(runtime_contracts) - set(descriptors))
    if unknown_runtime:
        raise ValueError(
            "runtime_contract 引用了未声明模块：" + "、".join(unknown_runtime)
        )

    for module_id, descriptor in descriptors.items():
        api_version = str(
            descriptor.get("api_version") or descriptor.get("version") or ""
        ).strip()
        if _version_tuple(api_version) is None:
            raise ValueError(f"模块 {module_id} 缺少有效 api_version")
        dynamic_versions[module_id] = api_version
        enabled = bool(descriptor.get("enabled", True))
        if enabled and module_id not in runtime_contracts:
            raise ValueError(f"模块 {module_id} 缺少 runtime_contract")
        if enabled:
            if module_id not in declared_features:
                raise ValueError(f"启用模块 {module_id} 缺少 protocol.features 声明")
            if str(declared_features[module_id]) != api_version:
                raise ValueError(
                    f"模块 {module_id} 的 feature 版本与 api_version 不一致"
                )
        else:
            if module_id in declared_features:
                raise ValueError(
                    f"已关闭模块 {module_id} 不得出现在 protocol.features"
                )
            if module_id in runtime_contracts:
                raise ValueError(
                    f"已关闭模块 {module_id} 不得保留 runtime_contract"
                )
        runtime = runtime_contracts.get(module_id)
        if runtime is None:
            continue
        expected_path = f"runtime.modules.{module_id}"
        state_path = str(runtime.get("state_path") or "")
        if state_path != expected_path:
            raise ValueError(
                f"模块 {module_id} 的 state_path 必须是 {expected_path}"
            )
    return dynamic_versions


def validate_world_contract(world: Mapping[str, Any]) -> dict[str, Any]:
    contract = world_contract(world)
    if contract["version"] not in SUPPORTED_WORLD_SCHEMA_VERSIONS:
        raise ValueError(
            f"{PLUGIN_VERSION} 只接受 TWP 内部世界模型修订 "
            f"{WORLD_SCHEMA_VERSION}，当前为 {contract['version']}"
        )
    protocol = contract["protocol"]
    if protocol.get("name", "").casefold() != "twp":
        raise ValueError("世界包必须声明 protocol.name = twp")
    if protocol.get("version") != TWP_VERSION:
        raise ValueError(f"世界包必须使用 TWP {TWP_VERSION}")
    if protocol.get("core") != TWP_CORE_VERSION:
        raise ValueError(
            f"世界包必须声明 protocol.core = {TWP_CORE_VERSION}"
        )
    display_tags = world.get("display_tags") or []
    if not isinstance(display_tags, Sequence) or isinstance(
        display_tags, (str, bytes)
    ):
        raise ValueError("display_tags 必须是预设标签键数组")
    normalized_tags = [str(value or "").strip() for value in display_tags]
    if len(normalized_tags) > 4:
        raise ValueError("display_tags 最多选择四项")
    if len(normalized_tags) != len(set(normalized_tags)):
        raise ValueError("display_tags 不得重复")
    unknown_tags = [
        value for value in normalized_tags if value not in WORLD_TAG_PRESETS
    ]
    if unknown_tags:
        raise ValueError(
            "display_tags 包含插件未预设的标签：" + "、".join(unknown_tags)
        )
    stats = contract["stats"]
    mode = stats["mode"]
    if mode == "preset_stack":
        minimum = _version_tuple(contract.get("minimum_plugin_version"))
        if minimum is None or minimum < (1, 0, 0):
            raise ValueError(
                "TWP 世界包必须声明 minimum_plugin_version >= 1.0.0"
            )
    if contract["version"] >= 5:
        minimum = _version_tuple(contract.get("minimum_plugin_version"))
        required_plugin = (1, 0, 0)
        if minimum is None or minimum < required_plugin:
            raise ValueError(
                f"世界包协议 v{contract['version']} 必须声明 minimum_plugin_version >= "
                f"{'.'.join(str(part) for part in required_plugin)}"
            )
        declared = protocol.get("features", {})
        module_versions = _validate_declared_modules(world, declared)
        for feature, raw_version in declared.items():
            supported = FEATURE_VERSIONS.get(str(feature))
            package_version = module_versions.get(str(feature))
            if supported is None and package_version is None:
                raise ValueError(f"插件不支持功能协议：{feature}")
            parsed = _version_tuple(raw_version)
            accepted = package_version or supported or ""
            if parsed is None or parsed > (_version_tuple(accepted) or (0, 0, 0)):
                raise ValueError(f"插件不支持 {feature}@{raw_version}")
        for requirement in contract["required_features"]:
            feature, minimum_feature = _feature_requirement(requirement)
            if feature not in declared:
                raise ValueError(f"缺少必需功能声明：{feature}")
            actual = _version_tuple(declared[feature])
            if actual is None or actual < minimum_feature:
                raise ValueError(f"功能版本不足：{requirement}")
        registry = EntityRegistry(world)
        for module_name in (
            "resources",
            "runtime_effects",
            "objects",
            "resolution_methods",
            "action_types",
        ):
            module_data = module_value(world, module_name, None)
            feature_name = "action_intents" if module_name == "action_types" else module_name
            if module_data not in (None, {}, []) and feature_name not in declared:
                raise ValueError(
                    f"声明 {module_name} 数据时必须启用 {feature_name} 功能版本"
                )
        resources_module = module_value(world, "resources", {})
        if isinstance(resources_module, Mapping):
            definitions = resources_module.get("definitions", resources_module.get("items", []))
            if isinstance(definitions, Sequence) and not isinstance(definitions, (str, bytes)):
                for definition in definitions:
                    if not isinstance(definition, Mapping):
                        raise ValueError("resources.definitions 每项必须是对象")
                    scope = str(definition.get("persistence_scope") or "session")
                    if scope not in PERSISTENCE_SCOPES:
                        raise ValueError(f"资源持久化作用域无效：{scope}")
                    if "initial_value" in definition:
                        try:
                            initial = float(definition["initial_value"])
                        except (TypeError, ValueError, OverflowError) as exc:
                            raise ValueError("资源 initial_value 必须是数值") from exc
                        value_range = definition.get("range")
                        if isinstance(value_range, Mapping):
                            minimum = value_range.get("min")
                            maximum = value_range.get("max")
                            if minimum is not None and initial < float(minimum):
                                raise ValueError("资源 initial_value 低于 range.min")
                            if maximum is not None and initial > float(maximum):
                                raise ValueError("资源 initial_value 超过 range.max")
        interaction = module_value(world, "interaction_rules", {})
        if isinstance(interaction, Mapping) and interaction.get("enabled"):
            if "interaction_rules" not in declared:
                raise ValueError("启用 interaction_rules 时必须声明对应功能版本")
            rules = interaction.get("rules", [])
            if not isinstance(rules, Sequence) or isinstance(rules, (str, bytes)):
                raise ValueError("interaction_rules.rules 必须是数组")
            ids: set[str] = set()
            for item in rules:
                if not isinstance(item, Mapping):
                    raise ValueError("interaction_rules.rules 每项必须是对象")
                rule_id = str(item.get("rule_id") or "")
                if not rule_id or rule_id in ids:
                    raise ValueError(f"交互规则 ID 缺失或重复：{rule_id or '<empty>'}")
                ids.add(rule_id)
                mode_value = str(item.get("mode") or "mechanical")
                if mode_value not in {"mechanical", "narrative", "hybrid"}:
                    raise ValueError(f"交互规则模式无效：{mode_value}")
                stacking = item.get("stacking")
                if isinstance(stacking, Mapping):
                    strategy = str(stacking.get("strategy") or "priority_only")
                    if strategy not in STACKING_STRATEGIES:
                        raise ValueError(f"交互规则叠加策略无效：{strategy}")
                for trigger in item.get("triggers", []):
                    if isinstance(trigger, Mapping) and str(trigger.get("phase") or "before_resolution") not in EVENT_PHASES:
                        raise ValueError(f"交互规则事件阶段无效：{trigger.get('phase')}")
                for effect in item.get("effects", []):
                    if isinstance(effect, Mapping) and str(effect.get("op") or "") not in OPERATION_TYPES:
                        raise ValueError(f"交互规则操作无效：{effect.get('op')}")
        capabilities_module = module_value(world, "capabilities", {})
        if isinstance(capabilities_module, Mapping) and capabilities_module:
            if "capabilities" not in declared:
                raise ValueError("声明 capabilities 数据时必须启用对应功能版本")
            cycles = CapabilityService(world, registry).progression_cycles()
            if cycles:
                raise ValueError("能力成长关系存在循环：" + " -> ".join(cycles[0]))
            operation_engine = OperationEngine(registry)
            transitions = capabilities_module.get("transitions", [])
            if isinstance(transitions, Sequence) and not isinstance(transitions, (str, bytes)):
                transition_ids: set[str] = set()
                for transition in transitions:
                    if not isinstance(transition, Mapping):
                        raise ValueError("capabilities.transitions 每项必须是对象")
                    transition_id = str(transition.get("transition_id") or "")
                    if not transition_id or transition_id in transition_ids:
                        raise ValueError(f"能力转换 ID 缺失或重复：{transition_id or '<empty>'}")
                    transition_ids.add(transition_id)
                    for source_ref in transition.get("from", []):
                        registry.resolve(source_ref, "capability")
                    operation_engine.validate(transition.get("operations", []))
        experience_module = module_value(world, "chat_experience", {})
        if isinstance(experience_module, Mapping) and experience_module:
            if "chat_experience" not in declared:
                raise ValueError(
                    "声明 chat_experience 数据时必须启用对应功能版本"
                )
            validate_chat_experience(world)
        fate_issues = validate_actor_fate(parse_actor_fate(world))
        terminal_issues = validate_terminal_conditions(
            parse_terminal_conditions(world),
            world,
        )
        contract_issues = [*fate_issues, *terminal_issues]
        if contract_issues:
            raise ValueError(
                "命运/终局契约无效：" + "；".join(contract_issues)
            )
    resolution_mode = contract["resolution"]["mode"]
    if mode == "none" and resolution_mode == "attribute":
        raise ValueError("无数值世界不能启用 attribute 属性检定")
    if mode == "none" and contract["attributes"]:
        raise ValueError("stats.mode=none 时不得声明角色属性")
    for risk, raw_dc in contract["resolution"]["difficulty_policy"].items():
        if raw_dc is None and risk == "safe":
            continue
        try:
            dc = int(raw_dc)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"difficulty_policy.{risk} 必须是整数") from exc
        if contract["version"] <= 4 and not 5 <= dc <= 25:
            raise ValueError(f"difficulty_policy.{risk} 必须介于 5—25")
    outcome_policy = contract["resolution"]["outcome_policy"]
    try:
        critical_margin = int(outcome_policy["critical_success_margin"])
        cost_floor = int(outcome_policy["cost_success_min_margin"])
        failure_floor = int(outcome_policy["failure_min_margin"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("outcome_policy 的结果档位必须是整数") from exc
    if critical_margin < 1 or not failure_floor < cost_floor < 0:
        raise ValueError("outcome_policy 的结果档位顺序无效")
    capabilities = contract["capabilities"]
    expected = {
        "character_stats": mode != "none",
        "attribute_checks": resolution_mode == "attribute",
        "dice_resolution": resolution_mode in {"dice_only", "attribute"},
    }
    for key, value in expected.items():
        if key in capabilities and bool(capabilities[key]) != value:
            raise ValueError(f"capabilities.{key} 与实际规则冲突")
    return contract


def attribute_lookup(
    contract: Mapping[str, Any],
    reference: str,
) -> tuple[str, str] | None:
    value = str(reference or "").strip().casefold()
    for item in contract.get("attributes", []):
        key = str(item.get("key") or "")
        label = str(item.get("label") or key)
        if value in {key.casefold(), label.casefold(), f"{label}检定".casefold()}:
            return key, label
    return None


__all__ = [
    "FEATURE_VERSIONS",
    "SUPPORTED_WORLD_SCHEMA_VERSIONS",
    "WORLD_SCHEMA_VERSION",
    "attribute_lookup",
    "shop_offers",
    "starter_loadout",
    "stats_mode",
    "validate_world_contract",
    "world_contract",
]
