from .registry import MessageDefinition, MessageSectionDefinition, _definition, register_message

register_message(
    _definition(
        "catalog.root",
        audience="public",
        title="321开团",
        summary="角色就位，世界开演。",
        sections=(
            (
                "text",
                "开一个新团\n{prefix} 开启\n\n"
                "加入当前副本\n{prefix} 加入\n\n"
                "查看我的角色与当前进度\n{prefix} 当前\n\n"
                "查看可用世界\n{prefix} 世界\n\n"
                "主持与管理\n{prefix} 主持\n\n"
                "需要完整说明\n{prefix} 帮助",
            ),
        ),
        delivery_policy="group",
    )
)

register_message(
    _definition(
        "session.closed",
        audience="public",
        title="副本已关闭",
        summary="《{session}》暂时停止接受新的故事和建卡输入。",
        sections=(
            ("text", "系统处理\n未完成的角色卡已安全挂起；投票、选择和计时已取消。"),
            ("text", "下一步\n管理员可以在“副本详情与控制”中重新开放。"),
        ),
    )
)

register_message(
    _definition(
        "session.reopened",
        audience="public",
        title="副本已重新开放",
        summary="《{session}》可以继续准备或开演。",
        sections=(
            ("text", "系统处理\n先前挂起的角色卡已经恢复，可以从原进度继续。"),
            ("text", "下一步\n相关玩家可私聊发送：\n{prefix} 当前"),
        ),
    )
)

register_message(
    _definition(
        "ending.aborted",
        audience="public",
        title="本轮已放弃",
        summary="《{session}》已永久归档，不会被误记为正常完结。",
        sections=(
            ("text", "原因：{reason}"),
            ("text", "系统处理\n已取消未完成建卡、投票、选择、计时和待执行操作。"),
            ("text", "下一步\n可以重新选择其他世界或创建新副本。"),
        ),
    )
)

register_message(
    _definition(
        "growth.available",
        audience="player",
        title="角色获得成长机会",
        summary="「{character}」满足了新的成长条件。",
        sections=(
            ("text", "成长依据\n{evidence}"),
            ("text", "下一步\n{prefix} 成长"),
        ),
        privacy="private",
        delivery_policy="private",
    )
)

register_message(
    _definition(
        "growth.confirmed",
        audience="player",
        title="成长已确认",
        summary="「{character}」已经获得〈{growth}〉。",
        sections=(
            ("text", "作用与场景\n{effect}"),
            ("text", "限制与条件\n{limit}"),
            ("text", "无需重复操作；系统已保存成长记录。"),
        ),
        privacy="private",
        delivery_policy="private",
    )
)

register_message(
    _definition(
        "delivery.partial",
        audience="player",
        title="消息部分送达",
        summary="已确认送达 {sent}/{total} 段。",
        sections=(
            ("text", "系统处理\n已保存下一段位置，重试不会重复已确认内容。"),
            ("text", "下一步\n{prefix} 当前"),
        ),
        privacy="private",
        delivery_policy="private",
    )
)

register_message(
    _definition(
        "delivery.retrying",
        audience="player",
        title="消息正在等待重试",
        summary="平台暂时没有确认后续内容送达。",
        sections=(
            ("text", "系统处理\n原操作不会重复执行，未送达内容保留在队列中。"),
            ("text", "下一步\n可以稍后私聊发送：\n{prefix} 当前"),
        ),
        privacy="private",
        delivery_policy="private",
    )
)

register_message(
    _definition(
        "delivery.permanently_failed",
        audience="player",
        title="消息未能送达",
        summary="平台多次拒绝或无法确认本次私聊。",
        sections=(
            ("text", "系统处理\n已停止自动重试，原业务结果仍然保留。"),
            ("text", "下一步\n请先重新建立私聊，再发送：\n{prefix} 当前"),
        ),
        privacy="private",
        delivery_policy="private",
    )
)

register_message(
    _definition(
        "delivery.cancelled",
        audience="player",
        title="消息投递已取消",
        summary="未送达部分不会继续发送。",
        sections=(("text", "原业务是否已完成，请发送以下命令查看：\n{prefix} 当前"),),
        privacy="private",
        delivery_policy="private",
    )
)

register_message(
    _definition(
        "turn.free_text",
        audience="player",
        title="请描述你的行动",
        summary="{prompt}",
        sections=(
            MessageSectionDefinition(
                slot="input_hint",
                source="world_text",
                copy_ref="input_hint",
                body="直接描述你想做什么、目标是什么，以及愿意承担的风险。",
                empty_policy="fallback",
                audience="player",
            ),
        ),
        privacy="private",
        delivery_policy="private",
    )
)

register_message(
    _definition(
        "turn.dialogue",
        audience="player",
        title="请回应当前对话",
        summary="{prompt}",
        sections=(
            MessageSectionDefinition(
                slot="input_hint",
                source="world_text",
                copy_ref="input_hint",
                body="直接说出角色的话，或描述语气、动作和目标。",
                empty_policy="fallback",
                audience="player",
            ),
        ),
        privacy="private",
        delivery_policy="private",
    )
)

register_message(
    _definition(
        "turn.notice",
        audience="public",
        title="{title}",
        summary="{prompt}",
        sections=(
            MessageSectionDefinition(
                slot="world_flavor",
                source="world_text",
                copy_ref="world_flavor",
                empty_policy="omit",
            ),
        ),
    )
)

register_message(
    _definition(
        "catalog.admin",
        audience="dm",
        title="321开团 · 主持与管理",
        summary="副本创建、准备、叙事控制与完结。",
        sections=(
            (
                "text",
                "创建或选择副本\n{prefix} 副本\n\n"
                "检查准备情况\n{prefix} 准备\n\n"
                "打开叙事控制\n{prefix} 主持\n\n"
                "暂停或恢复\n{prefix} 暂停\n{prefix} 恢复\n\n"
                "正常完结或强制终止\n{prefix} 完结\n{prefix} 终止",
            ),
        ),
        privacy="dm",
        delivery_policy="group",
    )
)

register_message(
    _definition(
        "instance.list",
        audience="public",
        title="可加入的副本 · 第 {page} 页",
        summary="共有 {count} 个副本可加入。",
        sections=(("text", "{instances}"),),
        delivery_policy="group",
        sensitive_fields=("instances",),
    )
)

register_message(
    _definition(
        "instance.detail",
        audience="public",
        title="副本详情",
        summary="{session}",
        sections=(("text", "世界：{world}\n状态：{state}\n人数：{count}\n主持：{host}"),),
        delivery_policy="group",
    )
)

register_message(
    _definition(
        "world.list",
        audience="public",
        title="可用世界",
        summary="回复序号查看世界说明。",
        sections=(("text", "{worlds}"),),
        delivery_policy="group",
    )
)

register_message(
    _definition(
        "world.detail",
        audience="public",
        title="世界说明",
        summary="{world}",
        sections=(
            ("text", "{description}"),
            ("text", "推荐：{player_limits}"),
        ),
        delivery_policy="group",
    )
)

register_message(
    _definition(
        "world.high_risk",
        audience="public",
        title="高难规则",
        summary="角色可能永久死亡。",
        sections=(
            (
                "text",
                "角色可能永久死亡。\n"
                "小队全部死亡时，副本立即失败并永久归档。\n"
                "本世界没有复活或回档覆盖结局。\n\n"
                "创建前请确认所有玩家能接受该规则。",
            ),
        ),
        delivery_policy="group",
    )
)

register_message(
    _definition(
        "instance.created",
        audience="public",
        title="副本已创建",
        summary="《{session}》已经进入准备阶段。",
        sections=(("text", "世界：{world}\n主持：{host}"),),
        delivery_policy="group",
    )
)

register_message(
    _definition(
        "instance.preparing",
        audience="public",
        title="准备已开放",
        summary="《{session}》现在接受加入。",
        delivery_policy="group",
    )
)

register_message(
    _definition(
        "join.success",
        audience="player",
        title="加入成功",
        summary="你已加入《{session}》的准备席位。",
        sections=(
            (
                "text",
                "我会尝试私聊发送建卡码。\n"
                "若 30 秒内没有收到，请先私聊 BOT 发送：\n\n{prefix} 建卡",
            ),
        ),
        privacy="private",
        delivery_policy="group",
    )
)

register_message(
    _definition(
        "join.left",
        audience="player",
        title="已退出副本",
        summary="你已退出《{session}》的准备席位。",
        privacy="private",
        delivery_policy="group",
    )
)

register_message(
    _definition(
        "card.code.sent",
        audience="player",
        title="《{session}》· 建卡入口",
        summary="你在群聊中的席位已经保留。",
        sections=(
            (
                "text",
                "建卡码：{code}\n"
                "有效期：{duration}\n\n"
                "请回复：\n{prefix} 建卡 {code}\n\n"
                "完成绑定前，这条私聊目标只用于发送建卡入口，"
                "不会被视为已验证身份。",
            ),
        ),
        privacy="private",
        delivery_policy="private",
        fallback_message_type="card.code.undelivered",
        sensitive_fields=("code",),
    )
)

register_message(
    _definition(
        "card.code.undelivered",
        audience="public",
        title="建卡入口未送达",
        summary="平台没有确认私聊消息已发送。",
        sections=(
            (
                "text",
                "系统已保留你的席位和建卡码。\n\n"
                "请先私聊 BOT 发送：\n{prefix} 建卡",
            ),
        ),
        delivery_policy="group",
    )
)

register_message(
    _definition(
        "card.bound",
        audience="player",
        title="私聊绑定成功",
        summary="后续建卡提醒与私密字段只通过这条私聊发送。",
        privacy="private",
        delivery_policy="private",
        fallback_message_type="card.code.undelivered",
    )
)

register_message(
    _definition(
        "card.field.prompt",
        audience="player",
        title="建卡 {step}/{total} · {field}",
        summary="{hint}",
        sections=(("text", "本页 {page} 项，共 {count} 项。\n回复当前页序号或完整名称。"),),
        privacy="private",
        delivery_policy="private",
        fallback_message_type="delivery.failed",
    )
)

register_message(
    _definition(
        "card.candidate.locked",
        audience="player",
        title="选项当前不可选",
        summary="〈{choice}〉当前不可选。",
        sections=(
            ("text", "原因\n{reason}"),
            ("text", "解锁方式\n{unlock}"),
            ("text", "系统没有修改你已经完成的其他字段。"),
        ),
        privacy="private",
        delivery_policy="private",
    )
)

register_message(
    _definition(
        "card.multiselect.hint",
        audience="player",
        title="请选择 {count} 项",
        summary="输入方式：序号用空格或逗号分隔。",
        sections=(("text", "示例\n{example}"),),
        privacy="private",
        delivery_policy="private",
    )
)

register_message(
    _definition(
        "card.completion_reminder",
        audience="player",
        title="建卡提醒",
        summary="你的角色卡还剩 {count} 项未完成：{fields}",
        sections=(
            ("text", "系统已保留当前进度。"),
            ("text", "{prefix} 当前"),
        ),
        privacy="private",
        delivery_policy="private",
        fallback_message_type="delivery.failed",
        sensitive_fields=("fields",),
    )
)

register_message(
    _definition(
        "card.preview",
        audience="player",
        title="角色卡预览",
        summary="「{character}」",
        sections=(("text", "{preview}"),),
        privacy="private",
        delivery_policy="private",
        sensitive_fields=("preview",),
    )
)

register_message(
    _definition(
        "card.confirmed",
        audience="public",
        title="角色卡已提交",
        summary="「{character}」已经进入审核。",
        sections=(
            ("text", "系统已保存\n公开角色卡、能力与资源、已确认的秘密字段。"),
            ("text", "接下来\n等待主持审核；如果需要修改，我会在私聊中说明具体字段和原因。"),
            ("text", "{prefix} 当前"),
        ),
        delivery_policy="group",
    )
)

register_message(
    _definition(
        "card.rejected",
        audience="player",
        title="角色卡未通过审核",
        summary="「{character}」需要修改后才能开演。",
        sections=(("text", "原因\n{reason}"),),
        privacy="private",
        delivery_policy="private",
        sensitive_fields=("reason",),
    )
)

register_message(
    _definition(
        "card.review_pending",
        audience="dm",
        title="待审核角色卡",
        summary="有 {count} 张角色卡等待审核。",
        privacy="dm",
        delivery_policy="group",
    )
)

register_message(
    _definition(
        "card.cancelled",
        audience="player",
        title="建卡已取消",
        summary="你的建卡进度已清空，席位仍然保留。",
        privacy="private",
        delivery_policy="private",
    )
)

register_message(
    _definition(
        "session.perform",
        audience="public",
        title="《{session}》· 开演",
        summary="世界：{world}",
        sections=(
            ("text", "队伍：{count} 人\n主持：{host}"),
            ("text", "第一幕：{act}"),
            ("text", "{opening}"),
        ),
        delivery_policy="group",
        sensitive_fields=("opening",),
    )
)

