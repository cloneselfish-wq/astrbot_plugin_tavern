from .common import *

def _context_compiler(
    budget: Mapping[str, Any],
) -> RelevantContextCompiler:
    key = tuple(
        sorted(
            (str(name), int(value))
            for name, value in budget.items()
            if isinstance(value, (int, float))
        )
    )
    compiler = _CONTEXT_COMPILERS.get(key)
    if compiler is None:
        compiler = RelevantContextCompiler(section_limits=dict(key))
        if len(_CONTEXT_COMPILERS) >= 16:
            _CONTEXT_COMPILERS.pop(next(iter(_CONTEXT_COMPILERS)))
        _CONTEXT_COMPILERS[key] = compiler
    return compiler


def _json(value: Any) -> str:
    # Prompt payloads are machine-readable; whitespace only consumes context.
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def compact_world_rules(world: Mapping[str, Any]) -> dict[str, Any]:
    """Compile only rules useful to narration; omit authoring/card payloads."""
    raw = world.get("rules", {})
    rules = raw if isinstance(raw, Mapping) else {}
    module_ids = {
        str(item.get("module_id") or item.get("id") or "")
        for item in world.get("twp_modules") or []
        if isinstance(item, Mapping)
    }
    result = {
        key: value
        for key, value in rules.items()
        if key in _NARRATIVE_RULE_KEYS
        and key not in _NON_NARRATIVE_RULE_KEYS
        and key not in module_ids
    }
    setting_modules = rules.get("setting_modules")
    if isinstance(setting_modules, Mapping):
        compact_modules = {
            key: value
            for key, value in setting_modules.items()
            if key in _NARRATIVE_SETTING_MODULE_KEYS
            and key not in _CARD_ONLY_SETTING_KEYS
        }
        if compact_modules:
            result["setting_modules"] = compact_modules
    return result


def _schema_for(*, allow_check: bool) -> dict[str, Any]:
    schema = json.loads(json.dumps(RESOLUTION_SCHEMA, ensure_ascii=False))
    if not allow_check:
        schema["mode"] = "resolve"
        schema.pop("check", None)
        schema["next_choices"][0]["check"] = (
            "null 或下一回合预先声明的检定；safe 必须为 null"
        )
    return schema


def _rules_digest_section(world: Mapping[str, Any]) -> str:
    """权威规则摘要区块；缺失或损坏时安全降级为空字符串。"""

    state = load_rules_digest(world)
    block = build_rules_digest_block(state)
    if not block:
        return ""
    return (
        "<world_rules_digest trust=\"world-package-authoritative\">\n"
        f"{block}\n"
        "</world_rules_digest>\n\n"
    )


def _capability_state_for_prompt(value: Any) -> dict[str, Any]:
    """只向模型投影能力的当前运行态，禁止泄漏未来成长等级。"""

    state = dict(value) if isinstance(value, Mapping) else {}
    growth = state.get("growth")
    if not isinstance(growth, Mapping):
        return state
    snapshot = growth.get("snapshot")
    snapshot = dict(snapshot) if isinstance(snapshot, Mapping) else {}
    current = {
        key: snapshot.get(key)
        for key in (
            "name",
            "summary",
            "effects",
            "costs",
            "limitations",
            "targets",
            "range",
            "duration",
            "commands",
            "ai_boundaries",
        )
        if snapshot.get(key) not in (None, "", [], {})
    }
    state["growth"] = {
        "level": int(growth.get("level") or 1),
        "level_name": str(
            growth.get("level_name") or snapshot.get("name") or ""
        ),
        "current": current,
    }
    return state


def system_prompt(
    world: Mapping[str, Any],
    *,
    allow_check: bool = True,
    capability_projection: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    """Purpose-built narrative system prompt without card-authoring duplication."""
    contract = world_contract(world)
    effective_allow_check = allow_check and contract["resolution"]["mode"] in {
        "dice_only",
        "attribute",
    }
    projection = [
        {
            "capability_ref": item.get("capability_ref"),
            "available": bool(item.get("available", True)),
            "state": _capability_state_for_prompt(item.get("state", {})),
        }
        for item in (capability_projection or ())
        if isinstance(item, Mapping) and item.get("capability_ref")
    ]
    capability_block = ""
    if projection:
        capability_block = (
            "<available_capabilities>\n"
            f"{_json(projection)}\n"
            "Only these projected capabilities may be narrated as currently usable. "
            "The plugin remains authoritative for costs, targets, constraints and effects.\n"
            "</available_capabilities>\n\n"
        )
    experience = narrator_directives(world)
    experience_block = (
        "<multiplayer_experience>\n"
        f"{_json(experience)}\n"
        "</multiplayer_experience>\n\n"
        if experience
        else ""
    )
    return (
        f"{CORE_NARRATOR_RULES}\n\n"
        "<world_definition>\n"
        f"{str(world.get('system_prompt', '')).strip()}\n"
        "</world_definition>\n\n"
        "<narrative_world_rules>\n"
        f"{_json(compact_world_rules(world))}\n"
        "</narrative_world_rules>\n\n"
        f"{_rules_digest_section(world)}"
        f"{capability_block}"
        f"{experience_block}"
        "<required_output_schema>\n"
        f"{_json(_schema_for(allow_check=effective_allow_check))}\n"
        "</required_output_schema>\n"
    )


def _history(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for event in events:
        result.append(
            {
                "turn": event.get("turn_no"),
                "role": event.get("role"),
                "actor_id": event.get("actor_id"),
                "actor_name": event.get("actor_name"),
                "content": event.get("content"),
            }
        )
    return result


__all__ = [name for name in globals() if not name.startswith('__')]

