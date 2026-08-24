from .registry import MessageDefinition, MessageSectionDefinition, _definition, register_message

register_message(
    _definition(
        "session.opening",
        audience="public",
        title="首轮行动",
        summary="{conflict}",
        sections=(
            MessageSectionDefinition(
                slot="world_flavor",
                source="world_text",
                copy_ref="world_flavor",
                empty_policy="omit",
            ),
            ("text", "可执行目标：{objective}"),
            ("text", "已知事实：{fact}"),
            ("text", "时间压力：{pressure}"),
            MessageSectionDefinition(
                slot="interaction_hint",
                source="runtime",
                body="{interaction_hint}",
                requires_data=("interaction_hint",),
                empty_policy="omit",
            ),
        ),
        delivery_policy="group",
    )
)

register_message(
    _definition(
        "turn.choices",
        audience="player",
        title="「{character}」的行动",
        summary="当前：{location}",
        sections=(
            ("text", "压力：《{clock}》{clock_value}"),
            ("text", "{choices}"),
            ("text", "回复 A、B 或 C。"),
        ),
        privacy="private",
        delivery_policy="private",
        sensitive_fields=("choices",),
    )
)

register_message(
    _definition(
        "turn.free_action",
        audience="public",
        title="自由行动",
        summary="「{character}」的行动权已交给「{next}」。",
        delivery_policy="group",
    )
)

register_message(
    _definition(
        "turn.check_result",
        audience="public",
        title="检定结果 · {outcome}",
        summary="{character}：{result}",
        sections=(
            ("text", "获得\n{gained}"),
            ("text", "代价\n{cost}"),
            ("text", "下一步\n行动权交给「{next}」。"),
        ),
        delivery_policy="group",
    )
)

register_message(
    _definition(
        "vote.open",
        audience="public",
        title="全队表决",
        summary="{question}",
        sections=(
            ("text", "{options}"),
            ("text", "回复：\n{prefix} 投票 {action}"),
        ),
        delivery_policy="group",
    )
)

register_message(
    _definition(
        "vote.result",
        audience="public",
        title="表决结果",
        summary="选择：{choice}",
        sections=(("text", "赞成 {support} 票 · 反对 {against} 票"),),
        delivery_policy="group",
    )
)

register_message(
    _definition(
        "timer.reminder",
        audience="public",
        title="{title}",
        summary="{subject}还剩 {duration}。",
        sections=(
            MessageSectionDefinition(
                slot="timeout_policy",
                kind="text",
                source="runtime",
                body="超时处理\n{result}",
                requires_data=("result",),
                empty_policy="omit",
            ),
        ),
        delivery_policy="group",
    )
)

register_message(
    _definition(
        "timer.timeout",
        audience="public",
        title="{title}",
        summary="{subject}已经到期。",
        sections=(
            ("text", "自动处理\n{result}"),
            MessageSectionDefinition(
                slot="next_step",
                kind="text",
                source="runtime",
                body="下一步\n{next_command}",
                requires_data=("next_command",),
                empty_policy="omit",
            ),
        ),
        delivery_policy="group",
    )
)

register_message(
    _definition(
        "ai_companion.awaiting_confirmation",
        audience="dm",
        title="AI 队友等待确认",
        summary="「{actor}」准备选择：{choice}",
        sections=(
            ("text", "判断依据\n{reason}"),
            ("text", "自动处理\n世界状态尚未改变，行动也没有提前提交。"),
            ("text", "下一步\n请在控制台确认、重选或暂停这名 AI 队友。"),
        ),
        delivery_policy="group",
    )
)

register_message(
    _definition(
        "ai_companion.submitted",
        audience="public",
        title="AI 队友已行动",
        summary="「{actor}」选择：{choice}",
        sections=(("text", "行动依据\n{reason}"),),
        delivery_policy="group",
    )
)

register_message(
    _definition(
        "ai_companion.failed",
        audience="dm",
        title="AI 队友行动未提交",
        summary="失败操作：提交「{actor}」的本轮选择。",
        sections=(
            ("text", "原因：{reason}"),
            ("text", "自动处理：世界状态保持不变，决策租约已经释放。"),
            ("text", "下一步\n请在控制台重试、重选或暂停这名 AI 队友。"),
        ),
        delivery_policy="group",
    )
)

register_message(
    _definition(
        "state.quest_updated",
        audience="public",
        title="任务更新",
        summary="《{quest}》",
        sections=(
            ("text", "进展\n{progress}"),
            ("text", "下一目标\n{next}"),
        ),
        delivery_policy="group",
    )
)

register_message(
    _definition(
        "state.clue_found",
        audience="public",
        title="获得新线索",
        summary="《{clue}》",
        sections=(("text", "{detail}"),),
        delivery_policy="group",
    )
)

register_message(
    _definition(
        "state.npc_status",
        audience="public",
        title="NPC 状态",
        summary="「{npc}」",
        sections=(
            ("text", "状态：{presence}"),
            ("text", "公开意图：{intent}"),
            ("text", "最近变化：{change}"),
        ),
        delivery_policy="group",
    )
)

register_message(
    _definition(
        "state.faction_changed",
        audience="public",
        title="阵营态势变化",
        summary="〖{faction}〗",
        sections=(
            ("text", "立场：{from_stance} → {to_stance}"),
            ("text", "原因：{reason}"),
            ("text", "当前诉求：{demand}"),
        ),
        delivery_policy="group",
    )
)

register_message(
    _definition(
        "state.scene_changed",
        audience="public",
        title="场景转移",
        summary="队伍进入〔{scene}〕",
        delivery_policy="group",
    )
)

register_message(
    _definition(
        "state.clock_changed",
        audience="public",
        title="时钟推进",
        summary="《{clock}》：{before} → {after}",
        delivery_policy="group",
    )
)

register_message(
    _definition(
        "state.resource_changed",
        audience="public",
        title="资源变化",
        summary="「{character}」使用〈{ability}〉。",
        sections=(
            ("text", "{resources}"),
            ("text", "限制\n{limits}"),
        ),
        delivery_policy="group",
    )
)

register_message(
    _definition(
        "state.item_granted",
        audience="public",
        title="获得道具",
        summary="『{item}』已经交给「{character}」。",
        delivery_policy="group",
    )
)

register_message(
    _definition(
        "state.relationship_changed",
        audience="public",
        title="关系变化",
        summary="「{character}」与「{target}」的关系：{before} → {after}",
        sections=(("text", "原因：{reason}"),),
        delivery_policy="group",
    )
)

register_message(
    _definition(
        "dm.control_switched",
        audience="public",
        title="叙事控制已切换",
        summary="主持模式：{mode}",
        sections=(
            ("text", "当前主持：「{host}」"),
            ("text", "AI 不再自动推进主叙事，但仍可提供规则核对和草案建议。"),
        ),
        delivery_policy="group",
    )
)

register_message(
    _definition(
        "dm.whisper",
        audience="player",
        title="主持密语",
        summary="这条信息只向你显示。",
        sections=(("text", "{content}"),),
        privacy="private",
        delivery_policy="private",
        fallback_message_type="dm.whisper_failed",
        sensitive_fields=("content",),
    )
)

register_message(
    _definition(
        "dm.whisper_failed",
        audience="dm",
        title="密语未送达",
        summary="平台没有确认密语已发送给「{target}」。",
        sections=(
            ("text", "消息已进入待投递队列，不会重复执行原操作。"),
            ("text", "请在 WebUI 查看投递状态，可以重试或取消。"),
        ),
        privacy="dm",
        delivery_policy="webui_only",
        fallback_message_type="delivery.failed",
    )
)

register_message(
    _definition(
        "dm.handoff",
        audience="public",
        title="行动权已交棒",
        summary="当前行动：「{actor}」",
        delivery_policy="group",
    )
)

register_message(
    _definition(
        "session.paused",
        audience="public",
        title="副本已暂停",
        summary="当前故事和倒计时已冻结。",
        sections=(
            ("text", "玩家输入不会推进回合。"),
            ("text", "恢复后将从《{scene}》继续。"),
        ),
        delivery_policy="group",
    )
)

register_message(
    _definition(
        "session.resumed",
        audience="public",
        title="副本已恢复",
        summary="故事从《{scene}》继续推进。",
        delivery_policy="group",
    )
)

register_message(
    _definition(
        "error.action_failed",
        audience="player",
        title="行动提交失败",
        summary="失败原因：{reason}",
        sections=(
            ("text", "系统处理\n本次行动没有写入，资源和回合均未消耗。"),
            ("text", "下一步\n请查看最新选项后重新选择。"),
            ("text", "{prefix} 当前"),
        ),
        privacy="private",
        delivery_policy="private",
        fallback_message_type="delivery.failed",
    )
)

register_message(
    _definition(
        "error.snapshot_stale",
        audience="public",
        title="任务状态暂时无法刷新",
        summary="正在显示 {time} 的上次成功数据。",
        sections=(
            ("text", "系统没有修改副本。"),
            ("text", "请稍后重试。"),
        ),
        delivery_policy="group",
    )
)

register_message(
    _definition(
        "delivery.failed",
        audience="player",
        title="私聊消息未送达",
        summary="平台没有确认消息已经发送。",
        sections=(
            ("text", "系统处理\n消息已进入待投递队列，不会重复执行原操作。"),
            ("text", "下一步\n请先私聊 BOT 发送：\n{prefix} 当前"),
        ),
        privacy="private",
        delivery_policy="private",
    )
)

register_message(
    _definition(
        "fate.critical",
        audience="public",
        title="「{character}」进入濒危",
        summary="原因：{reason}",
        sections=(
            ("text", "救援窗口\n下一次场景推进前，队友可以尝试救援。"),
            ("text", "可用资源\n{resources}"),
        ),
        delivery_policy="group",
    )
)

register_message(
    _definition(
        "fate.death",
        audience="public",
        title="「{character}」已经死亡",
        summary="救援窗口结束，角色生命状态已永久锁定。",
        sections=(
            ("text", "系统已处理\n取消该角色的行动与代控。\n保留遗言、遗物和最终回顾。"),
            ("text", "角色不能在本副本中复活或返场。"),
        ),
        delivery_policy="group",
    )
)

register_message(
    _definition(
        "fate.party_eliminated",
        audience="public",
        title="结局：《{ending}》",
        summary="小队已无存活角色。",
        sections=(
            ("text", "系统已完成\n取消所有行动、投票、计时和代控。\n"
                     "生成最终存档。\n将副本标记为失败并永久归档。"),
            ("text", "你仍可以查看\n最终时间线、角色结局、已发现线索和未完成任务。"),
            ("text", "本副本已只读，不能复活、回档或继续行动。"),
        ),
        delivery_policy="group",
    )
)

register_message(
    _definition(
        "archive.failed",
        audience="public",
        title="副本已失败归档",
        summary="《{session}》已经失败并永久归档。",
        sections=(
            ("text", "结局：《{ending}》"),
            ("text", "原因：{reason}"),
            ("text", "队伍结果\n存活 {living} 人 · 死亡 {dead} 人"),
            ("text", "完成\n任务 {quests_done}/{quests_total} · 线索 {clues_done}/{clues_total}"),
            ("text", "本副本已只读，不能复活、回档或继续行动。"),
        ),
        delivery_policy="group",
    )
)

register_message(
    _definition(
        "ending.completed",
        audience="public",
        title="故事完结：《{ending}》",
        summary="《{session}》已经结束并永久归档。",
        sections=(
            ("text", "结局摘要\n{summary}"),
            ("text", "队伍结果\n存活 {living} 人 · 死亡 {dead} 人"),
            ("text", "完成\n任务 {quests_done}/{quests_total} · 线索 {clues_done}/{clues_total}"),
            ("text", "查看复盘\n{prefix} 归档"),
        ),
        delivery_policy="group",
    )
)

register_message(
    _definition(
        "ending.recap",
        audience="public",
        title="副本复盘",
        summary="《{session}》",
        sections=(("text", "{recap}"),),
        delivery_policy="group",
        sensitive_fields=("recap",),
    )
)

