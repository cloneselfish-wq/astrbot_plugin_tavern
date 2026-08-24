from __future__ import annotations

from .registry import *
from .dashboard import *
from .runtime import *
from .worlds import *
from .operations import *
from .health import *
from .settings_contract import SETTING_ACTION_FIELDS



_SETTING_GROUP_LABELS = {
    "permissions": "权限边界",
    "model": "模型与裁定",
    "context": "上下文与节流",
    "time": "全局时间",
    "recovery": "数据与恢复",
    "panel": "独立 Web 面板",
}

_SETTING_FIELDS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "permissions": (
        ("security.admin_ids", "管理员", "text"),
        ("security.allowed_group_ids", "允许群组", "text"),
        ("security.require_group_whitelist", "限制可用群组", "boolean"),
        ("security.unauthorized_command_behavior", "未授权命令处理", "choice"),
        ("security.public_status", "允许公开状态查询", "boolean"),
        ("security.admin_count", "管理员数量", "number"),
        ("security.allowed_group_count", "允许群组数量", "number"),
    ),
    "time": (
        ("model.request_timeout_seconds", "单次模型等待", "duration"),
        ("model.generation_budget_total_seconds", "本轮总时间预算", "duration"),
        ("model.generation_budget_per_call_seconds", "单次生成预算", "duration"),
        ("runtime.user_cooldown_seconds", "玩家操作间隔", "duration"),
        ("runtime.story_generation_reminder_enabled", "故事生成进度提醒", "boolean"),
        ("runtime.story_generation_reminder_interval_seconds", "生成提醒间隔", "duration"),
    ),
    "model": (
        ("model.provider_id", "叙事模型", "choice"),
        ("model.fallback_provider_1_id", "第一备用模型", "choice"),
        ("model.fallback_provider_2_id", "第二备用模型", "choice"),
        ("model.fallback_provider_3_id", "第三备用模型", "choice"),
        ("model.fallback_provider_4_id", "第四备用模型", "choice"),
        ("model.image_caption_provider_id", "图片转述模型", "choice"),
        ("model.image_caption_prompt", "图片转述提示词", "text"),
        ("model.max_images_per_turn", "单回合图片上限", "number"),
        ("model.temperature", "随机度", "number"),
        ("model.max_tokens", "单次输出上限", "number"),
        ("model.request_timeout_seconds", "单次等待秒数", "number"),
        ("model.json_repair_attempts", "结构修复次数", "number"),
        ("model.generation_budget_total_seconds", "本轮总预算秒数", "number"),
        ("model.generation_budget_max_calls", "最多模型调用", "number"),
        ("model.generation_budget_per_call_seconds", "单次调用预算", "number"),
        ("model.generation_budget_max_fallbacks", "最多备用切换", "number"),
    ),
    "context": (
        ("runtime.default_world_slug", "默认世界", "text"),
        ("runtime.trigger_prefix", "命令前缀", "text"),
        ("runtime.qqbot_markdown_enabled", "Markdown 文本推送", "boolean"),
        ("runtime.max_input_chars", "单次输入字数上限", "number"),
        ("runtime.max_output_chars", "单次输出字数上限", "number"),
        ("runtime.recent_turns", "最近回合数量", "number"),
        ("runtime.memory_limit", "长期事实数量", "number"),
    ),
    "recovery": (
        ("token_quota.enabled", "启用 Token 配额", "boolean"),
        ("token_quota.window_seconds", "配额时间窗秒数", "number"),
        ("token_quota.token_limit", "默认 Token 上限", "number"),
        ("runtime.two_phase_checks", "提交前二阶段检查", "boolean"),
        ("runtime.auto_snapshot_interval", "自动存档间隔", "number"),
        ("advanced.audit_retention_days", "审计保留天数", "number"),
        ("advanced.store_model_payloads", "保存模型技术载荷", "boolean"),
    ),
    "panel": (
        ("remote_panel.enabled", "启用独立面板", "boolean"),
        ("remote_panel.host", "监听地址", "text"),
        ("remote_panel.port", "监听端口", "number"),
        ("remote_panel.allow_insecure_http", "允许不安全连接", "boolean"),
        ("remote_panel.secure_cookie", "只使用安全会话 Cookie", "boolean"),
    ),
}

_SETTING_SUMMARY_LABELS: dict[str, tuple[str, ...]] = {
    "permissions": (
        "限制可用群组",
        "允许群组数量",
        "管理员数量",
        "未授权命令处理",
        "允许公开状态查询",
    ),
    "model": (
        "叙事模型",
        "第一备用模型",
        "图片转述模型",
        "单回合图片上限",
        "随机度",
        "单次输出上限",
        "单次等待秒数",
        "结构修复次数",
        "本轮总预算秒数",
        "单次调用预算",
        "最多模型调用",
        "最多备用切换",
    ),
    "context": (
        "单次输入字数上限",
        "单次输出字数上限",
        "最近回合数量",
        "长期事实数量",
    ),
    "time": (
        "单次模型等待",
        "本轮总时间预算",
        "单次生成预算",
        "玩家操作间隔",
    ),
    "recovery": (
        "提交前二阶段检查",
        "自动存档间隔",
        "审计保留天数",
        "保存模型技术载荷",
    ),
    "panel": (
        "启用独立面板",
        "允许不安全连接",
        "只使用安全会话 Cookie",
    ),
}

_SETTING_GROUP_COPY = {
    "permissions": ("配置管理员、允许群组与未授权请求处理；变更会直接影响 BOT 和控制台访问边界。", "保存前重新核对身份与群范围，避免把当前管理员意外排除。"),
    "model": ("选择叙事模型与备用模型，并控制生成长度、随机度、等待时间和调用预算。", "模型与预算共同决定生成路径；可先执行延迟检测再保存。"),
    "context": ("控制默认世界、命令前缀、上下文容量与 Markdown 推送方式。", "Markdown 默认开启；不支持的平台仍使用同源纯文本。"),
    "time": ("集中管理模型等待、整轮预算、玩家节流与故事生成进度提醒。", "提醒间隔必须为 30—600 秒；超时不会覆盖已保存进度。"),
    "recovery": ("管理二阶段检查、自动存档、审计留存、模型载荷和默认 Token 配额。", "备份恢复先预览校验，再单独确认执行。"),
    "panel": ("管理独立 Web 面板的监听地址、端口与会话安全。", "地址或端口改变后需重新加载插件；公网访问应启用安全 Cookie。"),
}

_SETTING_NUMBER_CONSTRAINTS: dict[str, dict[str, int | float | str]] = {
    # These are the two editable floating-point settings in the WebUI contract.
    # HTML number inputs default to an integer step when this is omitted.
    "model.temperature": {"min": 0, "max": 2, "step": 0.1},
    "runtime.user_cooldown_seconds": {
        "min": 0,
        "max": 60,
        "step": 0.1,
        "unit": "秒",
    },
    "model.max_images_per_turn": {"min": 1, "max": 8, "step": 1, "unit": "张"},
    "runtime.story_generation_reminder_interval_seconds": {
        "min": 30,
        "max": 600,
        "step": 15,
        "unit": "秒",
    },
}

_SETTING_FIELD_COPY = {
    "security.admin_ids": "可管理插件的 AstrBot 用户标识，多个值用逗号分隔。",
    "security.allowed_group_ids": "群白名单开启后，仅这些群可使用命令，多个值用逗号分隔。",
    "model.provider_id": "当前叙事生成使用的主模型提供方标识。",
    "model.fallback_provider_1_id": "主模型失败时尝试的第一备用模型。",
    "model.fallback_provider_2_id": "第一备用模型不可用时继续尝试；留空表示跳过。",
    "model.fallback_provider_3_id": "第二备用模型不可用时继续尝试；留空表示跳过。",
    "model.fallback_provider_4_id": "第三备用模型不可用时最后尝试；留空表示跳过。",
    "model.image_caption_provider_id": "先把玩家本回合图片转成客观文字，再交给叙事模型；留空时拒绝带图行动。",
    "model.image_caption_prompt": "只约束图片转述，不会覆盖世界事实、裁定规则或玩家决定。",
    "model.max_images_per_turn": "每回合最多转述的图片数量，超出部分不会进入生成上下文。",
    "runtime.default_world_slug": "创建新副本时默认选用的已安装世界。",
    "runtime.trigger_prefix": "BOT 命令触发前缀；保存后玩家需使用新前缀。",
    "runtime.qqbot_markdown_enabled": "优先以 Markdown 推送；不支持的平台自动降级为纯文本。",
    "token_quota.enabled": "为新请求启用默认 Token 时间窗配额。",
    "token_quota.window_seconds": "配额统计时间窗，窗口结束后重新计数。",
    "token_quota.token_limit": "每个时间窗允许消耗的默认 Token 上限。",
    "remote_panel.host": "独立面板监听地址；仅本机访问建议 127.0.0.1。",
    "remote_panel.port": "独立面板监听端口，默认 8766。",
}


def _path_value(root: Mapping[str, Any], path: str) -> Any:
    current: Any = root
    for part in path.split("."):
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def _setting_value(root: Mapping[str, Any], path: str) -> Any:
    if path == "security.admin_count":
        declared = _mapping(root.get("security")).get("admin_count")
        if declared is not None:
            return _integer(declared, 0)
        return len(_sequence(_mapping(root.get("security")).get("admin_ids")))
    if path == "security.allowed_group_count":
        declared = _mapping(root.get("security")).get("allowed_group_count")
        if declared is not None:
            return _integer(declared, 0)
        return len(
            _sequence(_mapping(root.get("security")).get("allowed_group_ids"))
        )
    value = _path_value(root, path)
    if path in {"security.admin_ids", "security.allowed_group_ids"}:
        return "，".join(str(item) for item in _sequence(value))
    if path == "security.unauthorized_command_behavior":
        return "明确拒绝" if value == "deny" else "静默忽略"
    return value


def _settings_group_projection(
    selected_group: str,
    items: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    values = {str(item.get("label") or ""): item.get("value") for item in items}
    if selected_group == "permissions":
        return [
            {
                "label": "管理范围",
                "summary": (
                    f"{_integer(values.get('管理员数量'), 0)} 名管理员，"
                    f"{_integer(values.get('允许群组数量'), 0)} 个允许群组。"
                ),
                "state": "限制群组" if bool(values.get("限制可用群组")) else "不限群组",
            },
            {
                "label": "未授权请求",
                "summary": str(values.get("未授权命令处理") or "处理方式未确认"),
                "state": "明确策略",
            },
            {
                "label": "公开状态查询",
                "summary": "允许读取公开状态。" if bool(values.get("允许公开状态查询")) else "仅授权身份可读。",
                "state": "允许" if bool(values.get("允许公开状态查询")) else "限制",
            },
        ]
    labels_by_group = {
        "time": ("本轮总时间预算", "单次模型等待", "单次生成预算", "玩家操作间隔", "故事生成进度提醒", "生成提醒间隔"),
        "model": ("单次输出上限", "随机度", "结构修复次数", "最多备用切换"),
        "context": ("单次输入字数上限", "单次输出字数上限", "最近回合数量", "长期事实数量"),
        "recovery": ("提交前二阶段检查", "自动存档间隔", "审计保留天数", "保存模型技术载荷"),
        "panel": ("启用独立面板", "允许不安全连接", "只使用安全会话 Cookie"),
    }
    return [
        {
            "label": label,
            "summary": str(values.get(label)) if values.get(label) is not None else "当前值未配置。",
            "state": "当前策略",
        }
        for label in labels_by_group.get(selected_group, ())
    ]


def _settings_panel(
    context: SurfaceContext,
    settings: Mapping[str, Any],
    group_key: str,
    revision: int,
) -> dict[str, Any]:
    provider_options = [
        dict(option)
        for option in _sequence(_service(context.services, "provider_options"))
        if isinstance(option, Mapping) and option.get("value")
    ]
    items: list[dict[str, Any]] = []
    for path, label, control in _SETTING_FIELDS[group_key]:
        value = _setting_value(settings, path)
        item = {
            "key": context.key("setting", path),
            "object_kind": "setting",
            "label": label,
            "summary": _SETTING_FIELD_COPY.get(
                path,
                f"当前{label}来自服务端配置；保存时会校验取值与关联约束。",
            ),
            "state": "当前值",
            "value": value,
            "control": control,
            "editable": not path.endswith("_count"),
        }
        if control == "choice":
            item["value_label"] = _text(value, limit=80)
        items.append(item)
    editor_fields: list[dict[str, Any]] = []
    for alias, path, control in SETTING_ACTION_FIELDS[group_key]:
        field: dict[str, Any] = {
            "name": alias,
            "type": control,
            "labelKey": f"action.field.setting_{alias}",
            "value": _setting_value(settings, path),
            "label": next(
                (
                    label
                    for candidate_path, label, _ in _SETTING_FIELDS[group_key]
                    if candidate_path == path
                ),
                alias,
            ),
        }
        if control == "select" and alias == "unauthorized_behavior":
            field["required"] = True
            field["options"] = [
                {"value": "deny", "label": "明确拒绝"},
                {"value": "silent", "label": "静默忽略"},
            ]
            current = _path_value(settings, path)
            field["value"] = current if current in {"deny", "silent"} else "silent"
        elif control == "select":
            current = str(_path_value(settings, path) or "").strip()
            options = [{"value": "", "label": "跟随当前会话 / 不启用"}]
            options.extend(provider_options)
            if current and not any(
                str(option.get("value") or "") == current for option in options
            ):
                options.append({"value": current, "label": f"当前配置：{current}"})
            field["options"] = options
            field["required"] = False
            field["value"] = current
            field["hint"] = _SETTING_FIELD_COPY.get(path, "请选择 AstrBot 已配置的模型。")
        if alias == "image_caption_prompt":
            field["wide"] = True
            field["hint"] = _SETTING_FIELD_COPY[path]
        field.update(_SETTING_NUMBER_CONSTRAINTS.get(path, {}))
        editor_fields.append(field)
    actions = []
    if items:
        items[0]["key"] = context.key("setting", f"group:{group_key}")
        items[0]["revision"] = revision
        actions.append(
            _available_action(
                "settings.save",
                "settings.group.save",
                "修改当前设置组",
                target_kind="setting",
                expected_revision=revision,
                description="只保存当前分组；服务端会重新校验完整配置、回读持久结果并拒绝陈旧修订。",
                fields=editor_fields,
            )
        )
        if group_key == "recovery":
            actions.append(
                _available_action(
                    "E26",
                    "backup.restore.preview",
                    "上传并预览完整备份",
                    target_kind="setting",
                    expected_revision=revision,
                    description="先校验备份格式、清单与数据库完整性；预览不会替换当前数据。",
                    fields=[
                        {
                            "name": "file",
                            "type": "file",
                            "labelKey": "action.field.file",
                            "required": True,
                        }
                    ],
                )
            )
        items[0]["available_actions"] = actions
    items_by_label = {str(item.get("label") or ""): item for item in items}
    summary_items = [
        {
            "key": items_by_label[label]["key"],
            "label": label,
            "value": items_by_label[label].get("value"),
            "summary": items_by_label[label].get("summary"),
        }
        for label in _SETTING_SUMMARY_LABELS[group_key]
        if label in items_by_label
    ]
    panel = {
        "key": group_key,
        "label": _SETTING_GROUP_LABELS[group_key],
        "summary": _SETTING_GROUP_COPY[group_key][0],
        "impact_summary": _SETTING_GROUP_COPY[group_key][1],
        "conflict_summary": "保存使用当前修订号；若配置已被其他操作更新，系统保留草稿并要求刷新后重试。",
        "state": "未修改",
        "items": items,
        "summary_items": summary_items,
        "visual": _settings_group_projection(group_key, items),
        "available_actions": actions,
    }
    if group_key == "model":
        panel["model_chain"] = [
            str(_path_value(settings, path) or "").strip()
            for path in (
                "model.provider_id",
                "model.fallback_provider_1_id",
                "model.fallback_provider_2_id",
                "model.fallback_provider_3_id",
                "model.fallback_provider_4_id",
            )
            if str(_path_value(settings, path) or "").strip()
        ]
        panel["probe_action_label"] = "检测模型延迟"
        panel["probe_summary"] = "向当前模型链执行最小健康探测，并显示各模型的响应延迟与失败原因。"
    return panel


async def _settings_surface(context: SurfaceContext) -> SurfaceProjection:
    if "admin" not in context.roles:
        raise ForbiddenError(
            "安全与设置只允许 AstrBot 或独立面板管理员读取。",
            code="tavern.surface.settings_forbidden",
            recovery="请切换到管理员控制台身份后重试。",
        )
    source = _mapping(await _service_value(context.services, "settings"))
    if not source:
        raise ServiceUnavailableError(
            "设置投影服务暂时不可用。",
            code="tavern.surface.settings_unavailable",
            recovery="请刷新页面；若仍失败，请检查控制台设置服务。",
        )
    settings = _mapping(source.get("settings") or source)
    selected_group = (
        _text(context.query.get("group"), limit=40, default="permissions")
        or "permissions"
    ).lower()
    if selected_group not in _SETTING_FIELDS:
        raise BadRequestError(
            "设置分组无效。",
            code="tavern.surface.settings_group_invalid",
            recovery="请重新选择权限、时间、模型、上下文、恢复或面板。",
        )
    config_state = _mapping(source.get("config_state"))
    revision = config_state.get("revision")
    revision_number = _integer(revision, 0)
    panels = [
        _settings_panel(context, settings, group_key, revision_number)
        for group_key in _SETTING_GROUP_LABELS
    ]
    selected_panel = next(panel for panel in panels if panel["key"] == selected_group)
    return SurfaceProjection(
        data={
            "items": selected_panel["items"],
            "summary_items": selected_panel["summary_items"],
            "selected_group": selected_group,
            "filters": {"groups": list(_SETTING_GROUP_LABELS)},
            "groups": [
                {
                    "key": panel["key"],
                    "label": panel["label"],
                    "state": "当前" if panel["key"] == selected_group else "可选择",
                }
                for panel in panels
            ],
            "group_panels": panels,
            "impact": {
                "label": "保存前检查影响",
                "summary": "服务端会校验冲突、生效范围和是否需要重启。",
                "state": "未修改",
            },
            "policy": {
                "label": f"{selected_panel['label']}策略",
                "summary": "当前显示服务端裁剪后的可读设置；尚未提交修改。",
                "state": "未修改",
            },
        },
        summary={
            "label": "安全与设置",
            "summary": "六个设置组均来自当前服务端配置；可直接比较常用值与生效影响。",
            "state": "未修改",
            "count": len(panels),
        },
        revision=revision,
        updated_at=_text(config_state.get("updated_at"), limit=80),
        permissions={"can_view": True, "can_manage": True},
        empty=not panels,
    )


def _module_layer(value: Any) -> str:
    return {
        "core": "核心",
        "adapter": "适配",
        "interface": "界面",
        "infrastructure": "基础设施",
        "domain": "领域逻辑",
        "service": "服务",
        "security": "安全",
        "operations": "运营",
        "quality": "质量",
        "platform": "平台",
    }.get(_text(value, limit=50).lower(), "职责待确认")


async def _modules_surface(context: SurfaceContext) -> SurfaceProjection:
    manager = _service(context.services, "modules")
    if manager is None:
        raise ServiceUnavailableError(
            "模块目录暂时不可用。",
            code="tavern.surface.modules_unavailable",
            recovery="请刷新页面；若仍失败，请检查模块管理服务。",
        )
    catalog_method = getattr(manager, "catalog", None)
    raw_catalog = (
        await _maybe_await(catalog_method())
        if callable(catalog_method)
        else await _maybe_await(manager)
    )
    rows = [
        dict(item)
        for item in _sequence(raw_catalog)
        if isinstance(item, Mapping)
    ]
    from .module_categories import aggregate_module_catalog

    rows = aggregate_module_catalog(rows)
    labels = {
        _text(item.get("id"), limit=200): _safe_label(
            item.get("label"), "模块名称缺失"
        )
        for item in rows
    }
    consumer_refs = sorted(
        {
            _text(value, limit=200)
            for raw in rows
            for value in _sequence(raw.get("consumers"))
            if _text(value, limit=200) in labels
        },
        key=lambda value: labels.get(value, value),
    )
    consumer_options = [
        {
            "value": _opaque_filter_value(
                context, "module-consumer-filter", value
            ),
            "label": labels[value],
        }
        for value in consumer_refs
    ]
    projected: list[dict[str, Any]] = []
    consumer_refs_by_key: dict[str, set[str]] = {}
    layer_refs_by_key: dict[str, set[str]] = {}
    layer_options: dict[str, str] = {}
    for index, raw in enumerate(rows):
        internal = _text(raw.get("id"), limit=300)
        enabled = bool(raw.get("enabled"))
        raw_status = _text(raw.get("status"), limit=50).lower()
        raw_layer = _text(raw.get("layer"), limit=50).lower()
        raw_layers = {
            _text(value, limit=50).lower()
            for value in _sequence(raw.get("layer_keys"))
            if _text(value, limit=50)
        } or ({raw_layer} if raw_layer else set())
        for layer_key in raw_layers:
            layer_options[layer_key] = _module_layer(layer_key)
        state = (
            "已停用"
            if not enabled
            else "异常"
            if raw_status in {"failed", "error", "blocked"}
            else "可用"
            if raw_status in {"", "ready", "healthy"}
            else "需要关注"
            if raw_status == "attention"
            else "状态待确认"
        )
        dependencies = [
            labels[value]
            for value in (
                _text(item, limit=200) for item in _sequence(raw.get("dependencies"))
            )
            if value in labels
        ]
        consumers = [
            labels[value]
            for value in (
                _text(item, limit=200) for item in _sequence(raw.get("consumers"))
            )
            if value in labels
        ]
        projected_item = {
                "key": context.key("module", internal or f"module:{index}"),
                "object_kind": "module",
                "label": labels.get(internal, "模块名称缺失"),
                "summary": _public_text(
                    raw.get("description"),
                    limit=160,
                    default="该模块尚未提供可读职责说明。",
                ),
                "state": state,
                "layer": _module_layer(raw.get("layer")),
                "dependencies": dependencies,
                "consumers": consumers,
                "can_change": "admin" in context.roles
                and bool(raw.get("can_disable")),
                "updated_at": _text(raw.get("changed_at"), limit=80),
            }
        if "admin" in context.roles:
            projected_item["registry"] = [
                {
                    **dict(item),
                    "layer": _module_layer(_mapping(item).get("layer")),
                }
                for item in _sequence(raw.get("registry"))
                if isinstance(item, Mapping)
            ]
        projected.append(projected_item)
        layer_refs_by_key[projected_item["key"]] = raw_layers
        consumer_refs_by_key[projected_item["key"]] = {
            _text(value, limit=200)
            for value in _sequence(raw.get("consumers"))
            if _text(value, limit=200)
        }
    query = _text(context.query.get("q"), limit=200).casefold()
    wanted_state = _text(context.query.get("status"), limit=50).casefold()
    wanted_layer = _resolve_filter_value(
        context,
        "module-layer-filter",
        context.query.get("layer"),
        label="职责层",
    )
    wanted_consumer = _resolve_filter_value(
        context,
        "module-consumer-filter",
        context.query.get("consumer"),
        label="使用方",
    )
    status_options = sorted({item["state"] for item in projected})
    if query:
        projected = [
            item
            for item in projected
            if query in (item["label"] + " " + item["summary"]).casefold()
        ]
    if wanted_state:
        projected = [
            item for item in projected if item["state"].casefold() == wanted_state
        ]
    if wanted_layer:
        projected = [
            item for item in projected if wanted_layer in layer_refs_by_key.get(item["key"], set())
        ]
    if wanted_consumer:
        projected = [
            item
            for item in projected
            if wanted_consumer in consumer_refs_by_key.get(item["key"], set())
        ]
    offset, page_size = context.page(default=20)
    total = len(projected)
    items = projected[offset : offset + page_size]
    readonly = "admin" not in context.roles
    available = sum(1 for item in projected if item["state"] == "可用")
    attention = sum(1 for item in projected if item["state"] != "可用")
    detail: list[dict[str, Any]] = []
    if items:
        selected_item = items[0]
        if selected_item.get("dependencies"):
            detail.append(
                {
                    "label": "依赖",
                    "summary": "、".join(selected_item["dependencies"][:6]),
                    "state": f"{len(selected_item['dependencies'])} 项",
                }
            )
        if selected_item.get("consumers"):
            detail.append(
                {
                    "label": "使用方",
                    "summary": "、".join(selected_item["consumers"][:6]),
                    "state": f"{len(selected_item['consumers'])} 项",
                }
            )
    return SurfaceProjection(
        data={
            "items": items,
            "coverage": {
                "available": available,
                "attention": attention,
            },
            "context": {
                "label": "模块状态摘要",
                "summary": f"当前筛选内 {available} 个可用，{attention} 个需要关注。",
                "state": "需要关注" if attention else "可用",
                "updated_at": latest_timestamp(
                    *(item.get("updated_at") for item in items)
                ),
            },
            "filters": {
                "statuses": [
                    {"value": value, "label": value}
                    for value in status_options
                ],
                "layers": [
                    {
                        "value": _opaque_filter_value(
                            context, "module-layer-filter", value
                        ),
                        "label": label,
                    }
                    for value, label in layer_options.items()
                ],
                "consumers": consumer_options,
                "search": True,
            },
            "detail": detail,
            "pagination": _pagination(
                context,
                offset=offset,
                page_size=page_size,
                returned=len(items),
                total=total,
                has_more=offset + len(items) < total,
            ),
        },
        summary={
            "label": "系统模块",
            "summary": (
                f"六类模块中 {available} 类可用，{attention} 类需要关注。"
                if items
                else "调整筛选条件后重试。"
            ),
            "state": items[0]["state"] if items else "空",
            "count": total,
        },
        updated_at=latest_timestamp(*(item.get("updated_at") for item in items)),
        permissions={"can_view": True, "can_manage": not readonly},
        empty=not items,
        readonly=readonly,
    )


async def _about_surface(context: SurfaceContext) -> SurfaceProjection:
    source = _mapping(await _service_value(context.services, "about"))
    version = _public_text(
        source.get("version"), limit=80, default=PLUGIN_VERSION
    )
    support = _mapping(source.get("support"))
    features: list[dict[str, Any]] = []
    for state, values in (
        ("已支持", support.get("supported")),
        ("条件支持", support.get("conditional")),
        ("尚未验证", support.get("unverified")),
    ):
        for raw in _sequence(values)[:5]:
            label = _public_text(raw, limit=120)
            if label:
                features.append(
                    {
                        "key": context.key("about-feature", f"{state}:{label}"),
                        "label": label,
                        "summary": "以当前安装包与实际运行环境为准。",
                        "state": state,
                    }
                )
    if not features:
        features = [
            {
                "key": context.key("about-feature", "session-management"),
                "label": "跑团现场与副本管理",
                "summary": "围绕故事、角色、世界和投递生命周期协作。",
                "state": "当前能力",
            },
            {
                "key": context.key("about-feature", "player-delivery"),
                "label": "统一玩家消息出口",
                "summary": "玩家可见内容经统一 Markdown 与平台降级链路发送。",
                "state": "当前能力",
            },
            {
                "key": context.key("about-feature", "world-authoring"),
                "label": "世界与作者工具",
                "summary": "世界内容、规则检查和发布任务按权限使用。",
                "state": "当前能力",
            },
            {
                "key": context.key("about-feature", "mixed-party"),
                "label": "真人与 AI 队友",
                "summary": "以同级队伍信息展示行动状态、资源与可见背包。",
                "state": "当前能力",
            },
            {
                "key": context.key("about-feature", "delivery-recovery"),
                "label": "可靠投递与恢复",
                "summary": "分段发送并只恢复未确认内容，避免重复已经送达的正文。",
                "state": "当前能力",
            },
            {
                "key": context.key("about-feature", "role-console"),
                "label": "角色化控制台",
                "summary": "按管理员、作者、主持、玩家与只读身份裁剪页面和动作。",
                "state": "当前能力",
            },
        ]
    repository = _text(source.get("repository_url"), limit=300).rstrip("/")
    repository_allowlist = {
        "https://github.com/horizoe10/astrbot_plugin_tavern",
    }
    if repository not in repository_allowlist:
        repository = ""
    supported = [
        _public_text(value, limit=120)
        for value in _sequence(support.get("supported"))[:8]
        if _public_text(value, limit=120)
    ]
    conditional = [
        _public_text(value, limit=120)
        for value in _sequence(support.get("conditional"))[:8]
        if _public_text(value, limit=120)
    ]
    unverified = [
        _public_text(value, limit=120)
        for value in _sequence(support.get("unverified"))[:8]
        if _public_text(value, limit=120)
    ]
    if not supported and not conditional and not unverified:
        supported = ["本地控制台、Bot 命令与内置世界内容"]
        conditional = [
            "群聊消息取决于 AstrBot 与平台连接状态",
            "故事生成效果取决于当前选择的模型服务",
        ]
    support_projection = {
        "label": "支持边界",
        "summary": "本地功能由当前安装包提供；群聊连接与故事生成还取决于外部运行环境。",
        "supported": supported,
        "conditional": conditional,
        "unverified": unverified,
    }
    diagnostics = {
        "label": "脱敏支持摘要",
        "summary": (
            f"当前版本 {version}；已支持 {len(_sequence(support.get('supported')))} 项，"
            f"条件支持 {len(_sequence(support.get('conditional')))} 项，"
            f"尚未验证 {len(_sequence(support.get('unverified')))} 项。"
        ),
        "state": "不含账号、正文、提示词或追踪信息",
    }
    can_view_diagnostics = "admin" in context.roles
    return SurfaceProjection(
        data={
            "version": version,
            "support": support_projection,
            "features": features[:6],
            "resources": (
                [{"label": "项目仓库", "url": repository}]
                if repository
                else []
            ),
            **({"diagnostics": diagnostics} if can_view_diagnostics else {}),
        },
        summary={
            "label": "321开团",
            "summary": "面向群聊跑团的故事、世界与协作插件。",
            "state": version,
            "count": len(features),
        },
        permissions={
            "can_view": True,
            "can_manage": False,
            "can_view_diagnostics": can_view_diagnostics,
        },
    )


_SURFACE_SPECS: dict[str, SurfaceSpec] = {
    "dashboard": SurfaceSpec(
        "workspace_dashboard",
        frozenset({"admin", "author", "host", "player", "readonly"}),
        _dashboard_surface,
        manage_roles=frozenset({"admin", "author", "host"}),
    ),
    "tendencies": SurfaceSpec(
        "workspace_tendencies",
        frozenset({"player"}),
        _tendencies_surface,
        manage_roles=frozenset({"player"}),
    ),
    "sessions": SurfaceSpec(
        "workspace_sessions",
        frozenset({"admin", "host"}),
        _sessions_surface,
        manage_roles=frozenset({"admin", "host"}),
    ),
    "characters": SurfaceSpec(
        "workspace_characters",
        frozenset({"admin", "host"}),
        _characters_surface,
        manage_roles=frozenset({"admin", "host"}),
    ),
    "memories": SurfaceSpec(
        "workspace_memories",
        frozenset({"admin", "host"}),
        _memories_surface,
        manage_roles=frozenset({"admin", "host"}),
    ),
    "worlds": SurfaceSpec(
        "workspace_worlds",
        frozenset({"admin", "author"}),
        _worlds_surface,
        manage_roles=frozenset({"admin"}),
    ),
    "designer": SurfaceSpec(
        "workspace_designer",
        frozenset({"admin", "author"}),
        _designer_surface,
        manage_roles=frozenset({"admin", "author"}),
    ),
    "author_jobs": SurfaceSpec(
        "workspace_author_jobs",
        frozenset({"admin", "author"}),
        _author_jobs_surface,
        manage_roles=frozenset({"admin", "author"}),
    ),
    "todo": SurfaceSpec(
        "workspace_todo",
        frozenset({"admin", "host"}),
        _todo_surface,
        manage_roles=frozenset({"admin", "host"}),
    ),
    "audit": SurfaceSpec(
        "workspace_audit",
        frozenset({"admin"}),
        _audit_surface,
        manage_roles=frozenset({"admin"}),
    ),
    "health": SurfaceSpec(
        "workspace_health",
        frozenset({"admin"}),
        _health_surface,
        manage_roles=frozenset({"admin"}),
    ),
    "settings": SurfaceSpec(
        "workspace_settings",
        frozenset({"admin"}),
        _settings_surface,
        manage_roles=frozenset({"admin"}),
    ),
    "modules": SurfaceSpec(
        "workspace_modules",
        frozenset({"admin", "author"}),
        _modules_surface,
        manage_roles=frozenset({"admin"}),
    ),
    "about": SurfaceSpec(
        "workspace_about",
        frozenset({"admin", "author", "host", "player", "readonly"}),
        _about_surface,
        public=True,
    ),
}

# Public registration surface: host adapters can enumerate exact workspace keys
# without depending on the private policy table.
SURFACE_ROUTES: Mapping[str, SurfaceLoader] = MappingProxyType(
    {name: spec.loader for name, spec in _SURFACE_SPECS.items()}
)


def _error_response(kind: str, exc: BaseException) -> RouteResult:
    if isinstance(exc, WebRouteAdapterError):
        status, problem = _problem_from_adapter(exc)
    else:
        status, problem = problem_from_exception(exc)
    state = "permission" if status in {401, 403} else "conflict" if status == 409 else "error"
    envelope = visual_envelope(
        kind=kind,
        data={},
        revision=problem.preserved_revision,
        summary={
            "label": "当前板块不可用",
            "summary": problem.message,
            "state": state,
            "count": 0,
        },
        permissions={
            "can_view": state != "permission",
            "can_manage": False,
        },
        problems=[problem],
        state=state,
    )
    return {"status": int(status), "body": envelope.to_dict()}


def surface_error_response(workspace: str, exc: BaseException) -> RouteResult:
    """Return the same safe envelope when setup fails before route dispatch.

    Host configuration and service construction happen before
    ``route_surface_view``.  Mapping those failures here keeps the native
    plugin-page bridge from turning a structured server error into an opaque
    connection interruption.
    """

    name = _text(workspace, limit=80).lower()
    spec = _SURFACE_SPECS.get(name)
    kind = spec.kind if spec else "workspace_unknown"
    return _error_response(kind, exc)


async def route_surface_view(
    workspace: str,
    principal: Mapping[str, Any],
    database: Any,
    *,
    query: Mapping[str, Any] | None = None,
    services: Any = None,
) -> RouteResult:
    """Return one principal-scoped console workspace VisualEnvelope.

    ``database`` is the existing Tavern repository facade. ``services`` may be
    either a mapping or a host object; this module currently consumes the real
    module manager plus safe settings/about snapshots. Unknown workspaces,
    identities, roles and object handles fail closed.
    """

    name = _text(workspace, limit=80).lower()
    spec = _SURFACE_SPECS.get(name)
    kind = spec.kind if spec else "workspace_unknown"
    try:
        if spec is None:
            raise NotFoundError(
                "页面不存在或已经迁移。",
                code="tavern.surface.not_found",
                recovery="请返回六域导航后重新选择。",
            )
        principal_map = _mapping(principal)
        username = _text(principal_map.get("username"), limit=300)
        if not spec.public and not username:
            raise UnauthorizedError(
                "登录状态无效或已过期。",
                code="tavern.surface.login_required",
                recovery="请重新登录后返回当前工作区。",
            )
        roles = _principal_roles(principal_map)
        if not spec.public and not roles.intersection(spec.roles):
            raise ForbiddenError(
                "当前身份不能查看这个工作区。",
                code="tavern.surface.role_forbidden",
                recovery="请返回当前身份可见的工作区。",
            )
        normalized = QueryAdapter(
            _mapping(query),
            allowed_fields=_QUERY_FIELDS,
        ).normalize().to_mapping()
        filter_signature = "|".join(
            f"{key}={normalized[key]}"
            for key in sorted(normalized)
            if key not in {"cursor", "page_size", "expected_revision"}
        )
        scope = _principal_scope(principal_map)
        context = SurfaceContext(
            workspace=name,
            principal=principal_map,
            database=database,
            services=services,
            query=normalized,
            roles=roles,
            object_keys=OpaqueKeyFactory(scope=f"console-objects:{scope}"),
            cursor_keys=OpaqueKeyFactory(
                scope=f"console-surface:{scope}:{name}:{filter_signature}"
            ),
        )
        projection = await spec.loader(context)
        expected = _text(normalized.get("expected_revision"), limit=100)
        actual = projection.revision
        if expected and actual is not None and expected != str(actual):
            problem = VisualProblem(
                code="tavern.surface.revision_conflict",
                message="当前板块在你打开后已经更新。",
                recovery="请刷新并比较变化后重试。",
                retryable=True,
                preserved_revision=actual,
            )
            envelope = visual_envelope(
                kind=kind,
                data={"preserved": True},
                revision=actual,
                updated_at=projection.updated_at,
                summary={
                    "label": "数据已更新",
                    "summary": problem.message,
                    "state": "冲突",
                    "count": 1,
                },
                permissions={
                    "can_view": True,
                    "can_manage": False,
                },
                problems=[problem],
                state="conflict",
            )
            return {"status": 409, "body": envelope.to_dict()}
        projected_permissions = dict(projection.permissions)
        permissions = {
            **projected_permissions,
            "can_view": bool(projected_permissions.get("can_view", True)),
            # A loader may make a workspace more restrictive, but it may not
            # expand the central role policy declared by ``SurfaceSpec``.
            "can_manage": bool(roles.intersection(spec.manage_roles))
            and bool(projected_permissions.get("can_manage", True)),
        }
        resolved_state = projection.state
        resolved_readonly = projection.readonly
        if projection.empty and resolved_state is None:
            # Empty is a factual data result; it must not be hidden behind the
            # independent absence of write capability.
            resolved_state = "empty"
            resolved_readonly = False
        envelope = visual_envelope(
            kind=kind,
            data=projection.data,
            revision=projection.revision,
            updated_at=projection.updated_at,
            summary=projection.summary,
            permissions=permissions,
            problems=projection.problems,
            state=resolved_state,
            empty=projection.empty,
            stale=projection.stale,
            readonly=resolved_readonly,
        )
        return {"status": 200, "body": envelope.to_dict()}
    except Exception as exc:  # noqa: BLE001 - Web boundary maps safely
        return surface_error_response(name, exc)


__all__ = [name for name in globals() if not name.startswith('__')]
