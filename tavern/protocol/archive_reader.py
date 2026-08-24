from .common import *

def _compile_message_copy_contract(
    chat_definitions: Mapping[str, Any] | None,
    *,
    default_catalog: Mapping[str, str],
) -> tuple[dict[str, dict[str, str]], dict[str, Any]]:
    """Validate world copy bindings and freeze the interaction policy."""

    chat = dict(chat_definitions or {})
    config = chat.get("config")
    config = dict(config) if isinstance(config, Mapping) else chat
    declared = config.get("message_copy")
    declared = declared if isinstance(declared, Mapping) else {}
    bindings: dict[str, dict[str, str]] = {}
    for raw_message_type, raw_slots in sorted(declared.items()):
        message_type = str(raw_message_type or "").strip()
        definition = get_message(message_type)
        if definition is None:
            raise ValueError(f"message_copy 引用了未注册消息类型：{message_type}")
        slots = raw_slots if isinstance(raw_slots, Mapping) else {}
        allowed = {
            section.slot: section
            for section in definition.sections
            if isinstance(section, MessageSectionDefinition)
            and section.source == "world_text"
        }
        for raw_slot, raw_payload in sorted(slots.items()):
            slot = str(raw_slot or "").strip()
            if slot not in allowed:
                raise ValueError(
                    f"message_copy 插槽不受允许：{message_type}.{slot}"
                )
            payload = (
                raw_payload if isinstance(raw_payload, Mapping) else {}
            )
            text_id = str(payload.get("text_id") or "").strip()
            if not text_id:
                raise ValueError(
                    f"message_copy 缺少 text_id：{message_type}.{slot}"
                )
            candidates = (text_id, f"{text_id}.text", f"{text_id}.description")
            resolved_key = next(
                (item for item in candidates if item in default_catalog),
                "",
            )
            if not resolved_key:
                raise ValueError(
                    f"message_copy 文本未进入冻结目录：{message_type}.{slot}"
                )
            bindings.setdefault(message_type, {})[slot] = resolved_key
    for definition_type in tuple(bindings):
        definition = get_message(definition_type)
        if definition is None:
            continue
        for section in definition.sections:
            if (
                isinstance(section, MessageSectionDefinition)
                and section.source == "world_text"
                and section.required
                and section.slot not in bindings.get(definition_type, {})
            ):
                raise ValueError(
                    f"message_copy 缺少必填插槽："
                    f"{definition_type}.{section.slot}"
                )
    raw_policy = config.get("interaction_policy")
    raw_policy = raw_policy if isinstance(raw_policy, Mapping) else {}
    default_mode = str(
        raw_policy.get("default_mode") or "choices"
    ).strip().lower()
    opening_mode = str(
        raw_policy.get("opening_mode") or default_mode
    ).strip().lower()
    if default_mode not in INTERACTION_MODES:
        raise ValueError("interaction_policy.default_mode 无效")
    if opening_mode not in INTERACTION_MODES:
        raise ValueError("interaction_policy.opening_mode 无效")
    policy = {
        "default_mode": default_mode,
        "opening_mode": opening_mode,
        "resume_policy": dict(raw_policy.get("resume_policy") or {}),
    }
    return bindings, policy


def _issue(code: str, message: str, path: str = "", hint: str = "") -> TwpPackageError:
    return TwpPackageError(TwpValidationIssue(code, message, path, "error", hint))


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _validate_world_slug(world: Mapping[str, Any]) -> str:
    slug = str(world.get("slug") or "").strip()
    if not slug:
        raise _issue(
            "world.slug_missing",
            "world/core.json.slug 必填",
            "world/core.json.slug",
        )
    if len(slug) > 120 or not _SLUG_RE.fullmatch(slug):
        raise _issue(
            "world.slug_invalid",
            "world/core.json.slug 必须是小写稳定标识",
            "world/core.json.slug",
        )
    return slug


def _validate_scene_contracts(
    rules: Mapping[str, Any],
    world: Mapping[str, Any],
) -> str:
    graph = rules.get("scene_graph")
    if not isinstance(graph, Mapping):
        raise _issue(
            "world.scene_contract_missing",
            "世界缺少启用的 scene_graph 模块",
            "rules.scene_graph",
        )
    nodes = graph.get("nodes")
    if not isinstance(nodes, Sequence) or isinstance(nodes, (str, bytes)):
        raise _issue(
            "world.scene_contract_invalid",
            "scene_graph.nodes 必须是场景数组",
            "rules.scene_graph.nodes",
        )
    by_id: dict[str, Mapping[str, Any]] = {}
    for index, raw in enumerate(nodes):
        path = f"rules.scene_graph.nodes[{index}]"
        if not isinstance(raw, Mapping):
            raise _issue(
                "world.scene_contract_invalid",
                "正式场景必须是对象",
                path,
            )
        scene_id = str(raw.get("id") or "").strip()
        if not scene_id.startswith("scene:"):
            raise _issue(
                "world.scene_contract_invalid",
                "正式场景必须声明 scene: 稳定 ID",
                f"{path}.id",
            )
        if scene_id in by_id:
            raise _issue(
                "world.scene_contract_invalid",
                f"场景 ID 重复：{scene_id}",
                f"{path}.id",
            )
        by_id[scene_id] = raw
        for field in SCENE_CONTRACT_FIELDS:
            if field not in raw:
                raise _issue(
                    "world.scene_contract_missing",
                    f"场景 {scene_id} 缺少显式字段 {field}",
                    f"{path}.{field}",
                )
        transitions = raw.get("recommended_transitions")
        if not isinstance(transitions, Sequence) or isinstance(
            transitions,
            (str, bytes),
        ):
            raise _issue(
                "world.scene_contract_invalid",
                f"场景 {scene_id} 的 recommended_transitions 必须是对象数组",
                f"{path}.recommended_transitions",
            )
        for transition_index, transition in enumerate(transitions):
            transition_path = (
                f"{path}.recommended_transitions[{transition_index}]"
            )
            if not isinstance(transition, Mapping):
                raise _issue(
                    "world.scene_contract_invalid",
                    "场景转移必须是对象",
                    transition_path,
                )
            target = str(transition.get("scene_ref") or "").strip()
            if (
                not target.startswith("scene:")
                or not isinstance(transition.get("priority"), int)
                or not isinstance(transition.get("when"), Mapping)
            ):
                raise _issue(
                    "world.scene_contract_invalid",
                    "场景转移必须显式声明 scene_ref、整数 priority 和 when 对象",
                    transition_path,
                )
    if "opening_scene" in world:
        raise _issue(
            "world.opening_scene_legacy",
            "源码不得再使用 opening_scene，请改为 opening_scene_ref",
            "world/core.json.opening_scene",
        )
    opening_ref = str(world.get("opening_scene_ref") or "").strip()
    if not opening_ref.startswith("scene:"):
        raise _issue(
            "world.opening_scene_invalid",
            "opening_scene_ref 必须引用 scene: 场景",
            "world/core.json.opening_scene_ref",
        )
    opening = by_id.get(opening_ref)
    if opening is None:
        raise _issue(
            "world.opening_scene_missing",
            f"opening_scene_ref 引用不存在：{opening_ref}",
            "world/core.json.opening_scene_ref",
        )
    opening_value = (
        opening.get("opening_text")
        or opening.get("description")
        or opening.get("summary")
        or ""
    )
    if isinstance(opening_value, Mapping):
        opening_text = str(
            opening_value.get("text")
            or opening_value.get("description")
            or ""
        ).strip()
    else:
        opening_text = str(opening_value).strip()
    if not opening_text:
        raise _issue(
            "world.opening_scene_text_missing",
            "入口场景缺少 opening_text/description/summary",
            "rules.scene_graph.nodes",
        )
    return opening_text


__all__ = [name for name in globals() if not name.startswith('__')]

