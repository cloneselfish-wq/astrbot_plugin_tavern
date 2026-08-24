from .common import *
from .system import *
from .context import *
from .planning import *
from .resolution import *
from .repair import *

def choice_generation_prompt(
    *,
    world: Mapping[str, Any],
    session: Mapping[str, Any],
    participant: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    avoid: Sequence[Mapping[str, Any]] = (),
    validation_error: str = "",
    story_context: str = "",
) -> str:
    contract = world_contract(world)
    resolution_mode = str(contract["resolution"]["mode"])
    risk_policy = contract["resolution"]["difficulty_policy"]
    if resolution_mode in {"none", "narrative"}:
        check_rule = "当前世界不启用检定，四项 check 必须全部为 null。"
    elif resolution_mode == "dice_only":
        check_rule = (
            "当前世界使用纯骰检定；需要检定时 attribute_id 留空。"
        )
    else:
        attributes = "、".join(
            f"{item.get('key')}={item.get('label')}"
            for item in contract["attributes"]
        )
        check_rule = f"需要检定时 attribute_id 只能使用：{attributes}。"
    choice_world_state = (
        dict(session.get("world_state", {}))
        if isinstance(session.get("world_state"), Mapping)
        else {}
    )
    choice_world_state.pop("runtime", None)
    full_module_runtime = twp_runtime_projection(
        world, session.get("world_state", {})
    )
    module_runtime = full_module_runtime
    return (
        "只生成当前角色在当前场景可选的四个行动意图。"
        "必须恰好包含 A、B、C、D，至少一个 safe；actor_id 必须等于"
        " acting_character.participant_id。每项正文最多 50 字且不得自带"
        "风险或检定括号；不得预设成功，不得添加角色没有的能力、物品或知识。"
        "safe 代表没有显著风险与不确定性，check 必须为 null；"
        "controlled、dangerous、desperate、lethal 仅在结果确有不确定性时"
        "才可配置 check。DC 由插件按风险映射，模型填写值不具权威性。"
        f"{check_rule}"
        "致命风险必须明确已知后果；同一原因不能同时提高 DC 和造成劣势。"
        "只能描述当前角色本人能够尝试的行为，不得替其他玩家角色行动或决定。"
        "影响全队的转场、主线分支、共有资源或不可逆决定只能标记"
        " collective=true。返回单个 JSON 对象，不要解释。\n\n"
        "<world_definition>\n"
        f"{str(world.get('system_prompt', '')).strip()}\n"
        "</world_definition>\n\n"
        "<relevant_world_rules>\n"
        f"{_json(compact_world_rules(world))}\n"
        "</relevant_world_rules>\n\n"
        f"{_rules_digest_section(world)}"
        "<risk_dc_policy trust=\"plugin-authoritative\">\n"
        f"{_json(risk_policy)}\n"
        "</risk_dc_policy>\n\n"
        "<runtime_state>\n"
        f"{_json(choice_world_state)}\n"
        "</runtime_state>\n\n"
        "<world_module_runtime trust=\"plugin-authoritative\">\n"
        f"{_json(module_runtime)}\n"
        "</world_module_runtime>\n\n"
        "<entity_candidates trust=\"plugin-authoritative\">\n"
        f"{_json(item_candidate_projection(world, session, participant))}\n"
        "</entity_candidates>\n\n"
        "<acting_character>\n"
        f"{_json(_character_projection(participant, world))}\n"
        "</acting_character>\n\n"
        "<recent_history>\n"
        f"{_json(_history(list(events)[-8:]))}\n"
        "</recent_history>\n\n"
        "<avoid_repeating>\n"
        f"{_json([dict(item) for item in list(avoid)[-4:]])}\n"
        "</avoid_repeating>\n\n"
        "<resolved_story>\n"
        f"{str(story_context or '')[:3000]}\n"
        "</resolved_story>\n\n"
        "<previous_choice_error>\n"
        f"{str(validation_error or '')[:500]}\n"
        "</previous_choice_error>\n"
    )


def choice_repair_prompt(
    raw_output: str,
    error: str,
    *,
    world: Mapping[str, Any],
    participant: Mapping[str, Any],
) -> str:
    contract = world_contract(world)
    attributes = [
        {"id": item.get("key"), "label": item.get("label")}
        for item in contract["attributes"]
    ]
    return (
        "上一组选项未通过校验。只修复四个选项，不续写故事。"
        "必须保留 A、B、C、D；safe 的 check 必须为 null；"
        "actor_id 必须使用下面的权威 ID；属性只用稳定 ID。"
        "返回单个 JSON 对象，不要解释。\n\n"
        "<actor_id>"
        f"{_character_projection(participant, world).get('participant_id')}"
        "</actor_id>\n"
        "<allowed_attributes>"
        f"{_json(attributes)}"
        "</allowed_attributes>\n"
        "<risk_dc_policy>"
        f"{_json(contract['resolution']['difficulty_policy'])}"
        "</risk_dc_policy>\n"
        f"<validation_error>{json.dumps(error, ensure_ascii=False)}</validation_error>\n"
        "<invalid_output>\n"
        f"{raw_output[:8000]}\n"
        "</invalid_output>\n"
    )


__all__ = [name for name in globals() if not name.startswith('__')]

