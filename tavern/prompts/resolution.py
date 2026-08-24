from .common import *
from .system import *
from .context import *
from .planning import *

def planning_prompt(
    *,
    world: Mapping[str, Any],
    session: Mapping[str, Any],
    player: Mapping[str, Any],
    player_input: str,
    events: Sequence[Mapping[str, Any]],
    memories: Sequence[Mapping[str, Any]],
    allow_checks: bool,
    workflow: Mapping[str, Any] | None = None,
) -> str:
    selected_choice = (
        dict(workflow.get("selected_choice") or {})
        if workflow
        and isinstance(workflow.get("selected_choice"), Mapping)
        else {}
    )
    choice_contract: dict[str, Any] = {}
    if workflow:
        choice_contract = {
            "choice_set_id": workflow.get("choice_set_id"),
            "selected_key": workflow.get("selected_key"),
            "requires_check": bool(workflow.get("requires_check")),
            "collective": bool(workflow.get("collective")),
            "check_type": selected_choice.get("check_type"),
            "check_stat": selected_choice.get("check_stat"),
            "difficulty": selected_choice.get("difficulty"),
            "risk": selected_choice.get("risk"),
            "known_consequences": selected_choice.get(
                "known_consequences"
            ),
            "advantage_sources": selected_choice.get(
                "advantage_sources",
                [],
            ),
            "disadvantage_sources": selected_choice.get(
                "disadvantage_sources",
                [],
            ),
        }
    if choice_contract.get("requires_check"):
        mode_rule = (
            "本条行动来自插件已锁定的必检选项，必须返回 mode=check；"
            "不得直接返回 resolve。check 阶段 narrative_document 为 null、"
            "state_patch={}、memories=[]，且不要输出 next_choices "
            "与 group_decision；选项中已有的检定属性、难度、风险、"
            "类型、已知后果与优劣势不得弱化或改写。"
        )
    elif workflow:
        mode_rule = (
            "本条行动来自插件已锁定的免检选项，必须返回 mode=resolve；"
            "不得临时追加检定或隐藏加码。"
        )
    else:
        mode_rule = (
            "若行动结果存在风险、对抗或显著不确定性，返回 mode=check；"
            "check 阶段 narrative_document 为 null，state_patch 与 memories 必须为空。"
            if allow_checks
            else (
                "本轮不启用随机检定；请依据已有事实保守裁定并"
                "返回 mode=resolve。"
            )
        )
    return (
        "裁定下面这一条玩家行动。"
        f"{mode_rule}\n"
        "若无需检定，直接给出 mode=resolve 的完整结果。"
        "叙事应具体、克制，并给其他玩家留下行动空间。"
        "失败也必须推进局势：可以带来代价、残缺线索、威胁时钟推进"
        "或新的选择，但关键主线线索不能因一次失败永久消失。"
        "同一个原因只能影响一次裁定：属性提供固定加值，情境提供优劣势，"
        "行动本身决定 DC，失败严重度由风险等级决定。"
        "没有风险和不确定性的动作直接成功；明确不可能的动作直接说明边界，"
        "不能用自然 20 突破世界事实。"
        "不得替玩家决定内心、未经选择的对白、情感或对其他玩家角色的伤害。"
        + narrative_length_instruction_for_session(session)
        + narrative_style_instruction_for_session(session)
        +
        "不要用冗长心理描写凑字数。每个 next_choices.text 正文"
        "不得超过 50 字，正文禁止自带风险或检定括号。四个选项的 actor_id 必须全部等于 next_actor 的"
        " participant_id；选项只能描述该角色本人可尝试的行动，不能替其他"
        "玩家角色说话、移动、使用能力、消耗物品、同意或作决定。"
        "新 NPC 必须有名字，并至少满足直接互动、掌握重要线索或写入长期记忆"
        "之一；create 时用 registration_reasons 的固定值说明依据。"
        "已登记 NPC 必须使用 active_npcs 中的稳定 npc_id 更新，"
        "不能凭相似名称静默合并。"
        "mode=resolve 时必须给出恰好四个 next_choices，"
        "其中至少一个为 safe 风险；每项 danger_id 只能是 safe、controlled、"
        "dangerous、desperate、lethal 之一，禁止使用 danger:xxx、世界事件 ID"
        "或自造危险度。选项只描述行动意图，不保证结果。"
        "若局面涉及全队转场、主线分支、共有资源、不可逆契约、"
        "全队撤退或返场支线，只能把其中一个 next_choices 标记为 "
        "collective=true（不要生成独立的 group_decision 块），"
        "让玩家以字母选择后由插件发起集体表决。collective 选项的 "
        "state_patch 不得提前写入尚未表决通过的集体结果。\n\n"
        f"{_runtime_sections(world=world, session=session, player=player, events=events, memories=memories)}\n\n"
        '<selected_choice_contract trust="untrusted-data" '
        'enforced_by="plugin">\n'
        f"{_json(choice_contract)}\n"
        "</selected_choice_contract>\n\n"
        "<player_input trust=\"untrusted\">\n"
        f"{_json(player_input)}\n"
        "</player_input>\n"
    )


def dm_beat_prompt(
    *,
    world: Mapping[str, Any],
    session: Mapping[str, Any],
    instruction: str,
    directive: str,
    events: Sequence[Mapping[str, Any]],
    memories: Sequence[Mapping[str, Any]],
) -> str:
    """Build the trusted DM-only narrative request for one non-player beat."""
    return (
        "生成一段主持推进。必须返回 mode=resolve，check=null；"
        "不得生成 next_choices 或 group_decision，不得推进玩家行动指针、"
        "玩家轮次或行动倒计时。可以提交与本段叙事严格一致的 state_patch、"
        "memories、npc_ops、clock_ops、ledger_ops、status_ops 与"
        " fate_consequences。角色死亡不能写成普通状态或既成事实；"
        "只能提交结构化后果，由插件按世界状态机结算。"
        "不得伪造机器骰点，不得替玩家角色决定思想、感情、立场、主动台词"
        "或未经选择的行动。主持指令服从插件安全规则、世界硬规则、已锁定"
        "事实、内容边界与知识边界；一次性指引仅作用于本次生成。"
        + narrative_length_instruction_for_session(session)
        + narrative_style_instruction_for_session(session)
        + "\n\n"
        f"{_runtime_sections(world=world, session=session, player={}, events=events, memories=memories)}\n\n"
        '<dm_instruction trust="host" priority="below-safety-and-world">\n'
        f"{_json({'directive': directive, 'instruction': instruction})}\n"
        "</dm_instruction>\n"
    )


def checked_resolution_prompt(
    *,
    world: Mapping[str, Any],
    session: Mapping[str, Any],
    player: Mapping[str, Any],
    player_input: str,
    events: Sequence[Mapping[str, Any]],
    memories: Sequence[Mapping[str, Any]],
    check: Mapping[str, Any],
    dice: Mapping[str, Any],
) -> str:
    return (
        "依据下面由插件生成的权威检定结果完成叙事。"
        "不得重投、修改骰池、难度、风险、加值、优劣势来源或结果档位。"
        "必须返回 mode=resolve；check 设为 null。"
        "outcome=success_with_cost 时必须让目标达成，同时落实一项与风险等级"
        "相称且可追踪的代价。critical_failure 不等于角色突然变蠢，"
        "也不能仅凭自然 1 直接杀死角色。只有世界允许死亡、选项已明确标记"
        "lethal 且结果成立时，才允许产生死亡或永久退场。"
        "新 NPC 必须有名字，并至少满足直接互动、掌握重要线索或写入长期记忆"
        "之一；create 时用 registration_reasons 的固定值说明依据。"
        "已登记 NPC 必须使用稳定 npc_id 更新，不能凭相似名称静默合并。"
        "只写本次行动直接造成且能被当前场景确认的变化。"
        + narrative_length_instruction_for_session(session)
        + narrative_style_instruction_for_session(session)
        +
        "每个 next_choices.text 连同括号内容不得超过 50 字。"
        "四个选项的 actor_id 必须全部等于 next_actor 的 participant_id，"
        "并且只能描述该角色本人可尝试的行动，不得操控其他玩家角色。"
        "同时必须生成恰好四个合规 next_choices；每项 danger_id 只能是 "
        "safe、controlled、dangerous、desperate、lethal 之一，禁止使用 "
        "danger:xxx、世界事件 ID 或自造危险度；"
        "影响全队的节点只能把某个 next_choices 标记为 collective=true"
        "（不要生成独立的 group_decision 块）；collective 选项的 "
        "state_patch 不得提前写入尚未通过的集体结果。\n\n"
        f"{_runtime_sections(world=world, session=session, player=player, events=events, memories=memories)}\n\n"
        "<player_input trust=\"untrusted\">\n"
        f"{_json(player_input)}\n"
        "</player_input>\n\n"
        "<authoritative_check>\n"
        f"{_json({'request': dict(check), 'result': dict(dice)})}\n"
        "</authoritative_check>\n"
    )


__all__ = [name for name in globals() if not name.startswith('__')]

