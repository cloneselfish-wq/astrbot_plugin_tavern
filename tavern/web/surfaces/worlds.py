from __future__ import annotations

from .registry import *
from .dashboard import *
from .runtime import *
from .designer_matrix import designer_matrix_projection
from .world_projection import (
    _project_world,
    _world_author,
    _world_capability_entries,
    project_world_author_job,
)
from ...visualization.ui_profile import public_ui_profile
async def _worlds_surface(context: SurfaceContext) -> SurfaceProjection:
    rows = [
        dict(item)
        for item in await context.database.list_worlds(False) or ()
        if isinstance(item, Mapping)
    ]
    package_by_slug: dict[str, Mapping[str, Any]] = {}
    world_twp = _service(context.services, "world_twp")
    public_packages = getattr(world_twp, "public_packages", None)
    if callable(public_packages):
        try:
            for package in await _maybe_await(public_packages()) or ():
                if isinstance(package, Mapping):
                    package_by_slug[_text(package.get("slug"), limit=200)] = package
        except Exception:
            package_by_slug = {}
    default_world_slug = _text(
        _service(context.services, "default_world_slug"), limit=200
    )
    default_world_label = ""
    for raw in rows:
        if _text(raw.get("slug"), limit=200) == default_world_slug:
            default_world_label = _safe_label(
                raw.get("name"), "世界名称缺失"
            )
            break
    from ...protocol.constants import MODULE_METADATA

    projected_records: list[dict[str, Any]] = []
    for raw in rows:
        package = package_by_slug.get(_text(raw.get("slug"), limit=200), {})
        # Database rows intentionally keep a compact runtime projection. Core
        # gameplay documentation belongs to the compiled package revision, so
        # merge only that trusted installed artifact before public projection.
        projection_source = dict(raw)
        package_ref = _text(package.get("package_ref"), limit=300)
        compiled_world = getattr(world_twp, "compiled_world", None)
        compiled: Mapping[str, Any] = {}
        if package_ref and callable(compiled_world):
            try:
                compiled = await _maybe_await(compiled_world(package_ref))
                if isinstance(compiled, Mapping):
                    for key in (
                        "elemental", "rules", "runtime_contract", "capability_index",
                        "ui_profile", "gameplay_brief", "description",
                        "recommended_players", "display_tags", "enabled_modules",
                        "twp_modules", "required_features",
                    ):
                        if key in compiled:
                            projection_source[key] = compiled[key]
            except Exception:
                # The world remains listable; the detail receipt will identify
                # which package-backed gameplay sections are unavailable.
                pass
        item = _project_world(context, projection_source, package)
        compiled_revision = _text(
            compiled.get("artifact_hash") or compiled.get("artifact_id"), limit=200
        ) or str(_integer(raw.get("revision"), 0))
        item["world_key"] = item["key"]
        item["compiled_revision"] = compiled_revision
        item["actions"] = {
            "open_details": {
                "intent": "world.details.read",
                "target": "worlds",
                "world_key": item["key"],
                "expected_revision": compiled_revision,
                "cache_policy": "private",
            },
            "read_world_setting": {
                "intent": "world.readme.index.read",
                "target": "world-twp/readme",
                "world_key": item["key"],
                "expected_revision": compiled_revision,
                "cache_policy": "private",
            },
        }
        item["is_default"] = bool(
            default_world_slug
            and _text(raw.get("slug"), limit=200) == default_world_slug
        )
        internal = _text(raw.get("id") or raw.get("slug"), limit=300)
        revision = _integer(raw.get("revision"), 0)
        actions: list[dict[str, Any]] = []
        module_options: list[dict[str, Any]] = []
        if "admin" in context.roles and internal and revision > 0:
            package_ref = _text(package.get("package_ref"), limit=300)
            for index, raw_module in enumerate(_sequence(package.get("modules"))):
                module = _mapping(raw_module)
                module_id = _text(
                    module.get("module_id") or module.get("id"), limit=200
                )
                if not package_ref or not module_id or bool(module.get("required")):
                    continue
                metadata = MODULE_METADATA.get(module_id, ())
                label = _public_text(
                    metadata[0] if metadata else "", limit=100,
                    default=f"可选模块 {index + 1}",
                )
                state = "已启用" if bool(module.get("enabled", True)) else "已停用"
                module_options.append(
                    {
                        "value": context.key(
                            "world-module",
                            f"{internal}\x1f{package_ref}\x1f{module_id}",
                        ),
                        "label": f"{label}（{state}）",
                    }
                )
            if module_options:
                actions.append(
                    _available_action(
                        "E25",
                        "world.module.toggle",
                        "调整可选世界模块",
                        target_kind="world",
                        expected_revision=revision,
                        description="只列出编译器确认可安全关闭的模块；提交失败会恢复世界包原状态。",
                        fields=[
                            {
                                "name": "module_key",
                                "type": "select",
                                "labelKey": "action.field.module",
                                "required": True,
                                "options": module_options,
                            },
                            {
                                "name": "enabled",
                                "type": "checkbox",
                                "labelKey": "action.field.enabled",
                            },
                        ],
                    )
                )
            if _text(raw.get("slug"), limit=200) != default_world_slug:
                actions.append(
                    _available_action(
                        "C05",
                        "world.archive",
                        "归档这个世界",
                        target_kind="world",
                        expected_revision=revision,
                        description="仅在没有活动副本使用时归档；内容与修订历史会保留。",
                        fields=[
                            {
                                "name": "acknowledge_archive",
                                "type": "checkbox",
                                "labelKey": "action.field.acknowledge_archive",
                                "required": True,
                            }
                        ],
                    )
                )
        item["available_actions"] = actions
        item["readonly"] = not bool(actions)
        author_label = _world_author(raw)
        capability_entries = _world_capability_entries(projection_source)
        item["author"] = author_label
        item["capability_summary"] = [
            {
                "label": entry["label"],
                "state": "可用" if entry["enabled"] else "未启用",
                "summary": entry.get("summary", ""),
            }
            for entry in capability_entries
        ]
        projected_records.append(
            {
                "item": item,
                "internal": internal,
                "author": author_label,
                "capabilities": capability_entries,
            }
        )
    status_options = sorted(
        {record["item"]["state"] for record in projected_records}
    )
    author_values = sorted(
        {record["author"] for record in projected_records if record["author"]}
    )
    capability_values: dict[str, str] = {}
    for record in projected_records:
        for entry in record["capabilities"]:
            capability_values.setdefault(entry["key"], entry["label"])
    author_options = [
        {
            "value": _opaque_filter_value(
                context, "world-author-filter", value
            ),
            "label": value,
        }
        for value in author_values
    ]
    capability_options = [
        {
            "value": _opaque_filter_value(
                context, "world-capability-filter", key
            ),
            "label": label,
        }
        for key, label in sorted(
            capability_values.items(), key=lambda item: item[1]
        )
    ]
    query = _text(context.query.get("q"), limit=200).casefold()
    wanted_state = _text(context.query.get("status"), limit=50).casefold()
    wanted_author = _resolve_filter_value(
        context,
        "world-author-filter",
        context.query.get("author"),
        label="作者",
    )
    wanted_capability = _resolve_filter_value(
        context,
        "world-capability-filter",
        context.query.get("capability"),
        label="能力",
    )
    if query:
        projected_records = [
            record
            for record in projected_records
            if query
            in (
                record["item"]["label"]
                + " "
                + record["item"]["summary"]
                + " "
                + record["author"]
            ).casefold()
        ]
    if wanted_state:
        projected_records = [
            record
            for record in projected_records
            if record["item"]["state"].casefold() == wanted_state
        ]
    if wanted_author:
        projected_records = [
            record for record in projected_records if record["author"] == wanted_author
        ]
    if wanted_capability:
        projected_records = [
            record
            for record in projected_records
            if wanted_capability
            in {entry["key"] for entry in record["capabilities"]}
        ]
    offset, page_size = context.page(default=8)
    total = len(projected_records)
    page_records = projected_records[offset : offset + page_size]
    author_job_problems: list[VisualProblem] = []
    list_author_jobs = getattr(context.database, "list_author_jobs", None)
    can_view_author_jobs = bool(context.roles & {"admin", "author"})
    for record in page_records:
        item = record["item"]
        item["author_jobs"] = []
        if not can_view_author_jobs:
            item["author_jobs_access"] = "restricted"
            continue
        if not callable(list_author_jobs):
            item["author_jobs_access"] = "unavailable"
            continue
        try:
            author_jobs = await _maybe_await(
                list_author_jobs(world_ref=record["internal"], limit=5)
            )
        except Exception:
            item["author_jobs_access"] = "unavailable"
            author_job_problems.append(
                VisualProblem(
                    code="tavern.surface.world_author_jobs_unavailable",
                    message=f"《{item['label']}》的作者任务产物暂时无法读取。",
                    recovery="请刷新世界库；仍失败时前往作者任务页检查服务状态。",
                    retryable=True,
                )
            )
            continue
        item["author_jobs"] = [
            project_world_author_job(raw_job)
            for raw_job in author_jobs or ()
            if isinstance(raw_job, Mapping)
        ]
        item["author_jobs_access"] = (
            "available" if item["author_jobs"] else "empty"
        )
    items = [record["item"] for record in page_records]
    import_sources: list[dict[str, Any]] = []
    if "admin" in context.roles:
        import_sources.append(
            {
                "key": context.key("github-source", "public-github-worlds"),
                "object_kind": "github-source",
                "label": "从公开 GitHub 仓库导入",
                "summary": "先扫描候选，再选择一个完整世界内容包体检并安装。",
                "state": "等待仓库地址",
                "revision": 1,
                "available_actions": [
                    _available_action(
                        "E28",
                        "github.world.preview",
                        "扫描 GitHub 世界包",
                        target_kind="github-source",
                        expected_revision=1,
                        description="只访问公开 GitHub API 与白名单下载主机；扫描不会安装内容。",
                        fields=[
                            {
                                "name": "repo_url",
                                "type": "url",
                                "labelKey": "action.field.github_repository",
                                "required": True,
                            },
                            {
                                "name": "branch",
                                "type": "text",
                                "labelKey": "action.field.github_branch",
                            },
                        ],
                    )
                ],
            }
        )
        list_previews = getattr(context.database, "list_github_import_previews", None)
        if callable(list_previews):
            route_actor_id = (
                _text(context.principal.get("username"), limit=300)
                or "web:anonymous"
            )
            previews = await _maybe_await(
                list_previews(route_actor_id, limit=5)
            )
            for preview in previews or ():
                value = _mapping(preview)
                operation_id = _text(value.get("operation_id"), limit=300)
                revision = _integer(value.get("revision"), 0)
                candidates = [
                    _mapping(candidate)
                    for candidate in _sequence(value.get("candidates"))
                    if isinstance(candidate, Mapping)
                ]
                if not operation_id or revision < 1 or not candidates:
                    continue
                import_sources.append(
                    {
                        "key": context.key("github-preview", operation_id),
                        "object_kind": "github-preview",
                        "label": _safe_label(
                            value.get("repository_label"), "GitHub 导入预览"
                        ),
                        "summary": f"已保留 {len(candidates)} 个候选，可在重启后继续安装。",
                        "state": "等待确认",
                        "revision": revision,
                        "available_actions": [
                            _available_action(
                                "E28",
                                "github.world.commit",
                                "继续安装世界包",
                                target_kind="github-preview",
                                expected_revision=revision,
                                description="使用已保存的扫描结果继续；下载或安装失败时仍可重试。",
                                fields=[
                                    {
                                        "name": "candidate_index",
                                        "type": "select",
                                        "labelKey": "action.field.github_candidate",
                                        "required": True,
                                        "options": [
                                            {
                                                "value": index,
                                                "label": _safe_label(
                                                    candidate.get("name"),
                                                    f"世界包候选 {index + 1}",
                                                ),
                                            }
                                            for index, candidate in enumerate(candidates)
                                        ],
                                    },
                                    {
                                        "name": "acknowledge_install",
                                        "type": "checkbox",
                                        "labelKey": "action.field.acknowledge_install",
                                        "required": True,
                                    },
                                ],
                            )
                        ],
                    }
                )
    matrix_capabilities: dict[str, str] = {}
    for record in page_records:
        for entry in record["capabilities"]:
            matrix_capabilities.setdefault(entry["key"], entry["label"])
    capability_matrix = {
        "columns": [
            {"key": item["key"], "label": item["label"]}
            for item in items[:4]
        ],
        "rows": [
            {
                "key": _opaque_filter_value(
                    context, "world-capability-filter", capability_key
                ),
                "label": capability_label,
                "cells": [
                    {
                        "state": (
                            "可用"
                            if any(
                                entry["key"] == capability_key
                                and entry["enabled"]
                                for entry in record["capabilities"]
                            )
                            else "未启用"
                            if any(
                                entry["key"] == capability_key
                                for entry in record["capabilities"]
                            )
                            else "未声明"
                        )
                    }
                    for record in page_records[:4]
                ],
            }
            for capability_key, capability_label in list(
                sorted(matrix_capabilities.items(), key=lambda item: item[1])
            )[:10]
        ],
    }
    return SurfaceProjection(
        data={
            "items": items,
            "import_sources": import_sources,
            "capabilities": capability_matrix,
            "filters": {
                "statuses": [
                    {"value": value, "label": value}
                    for value in status_options
                ],
                "authors": author_options,
                "capabilities": capability_options,
                "search": True,
            },
            "changes": [
                {
                    "label": item["label"],
                    "summary": "世界资料的可见快照已更新。",
                    "state": item["state"],
                    "created_at": item.get("updated_at"),
                }
                for item in items
                if item.get("updated_at")
            ][:5],
            "context": {
                "default_world_label": default_world_label,
            },
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
            "label": "世界库",
            "summary": (
                "浏览已安装世界、查看可玩内容，并在安装前检查世界包。"
                if items
                else "安装或创建世界后，这里会显示可玩内容。"
            ),
            "state": "可用" if items else "空",
            "count": total,
            "default_world_label": default_world_label,
        },
        revision=None,
        updated_at=latest_timestamp(*(item.get("updated_at") for item in items)),
        permissions={
            "can_view": True,
            "can_manage": "admin" in context.roles,
            "can_author": bool(context.roles & {"admin", "author"}),
        },
        problems=author_job_problems,
        state="empty" if not items else "partial" if author_job_problems else None,
        empty=not items,
        readonly="admin" not in context.roles and bool(items),
    )


def _designer_issue_counts(report: Mapping[str, Any]) -> tuple[int, int, int]:
    report = _mapping(report)
    return (
        len(_sequence(report.get("errors"))),
        len(_sequence(report.get("warnings"))),
        len(_sequence(report.get("suggestions"))),
    )


def _designer_flow_projection(
    context: SurfaceContext,
    world_ref: str,
    report: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    template = _mapping(_mapping(report).get("actor_template"))
    fields = [
        _mapping(item)
        for item in _sequence(template.get("fields"))
        if isinstance(item, Mapping)
    ]
    nodes: list[dict[str, Any]] = []
    preview: list[dict[str, Any]] = []
    internal_keys: list[str] = []
    for index, field in enumerate(fields):
        label = _public_text(field.get("label") or field.get("name"), limit=100)
        internal = _text(field.get("key") or field.get("id"), limit=200)
        if not label or not internal:
            continue
        key = context.key("designer-node", f"{world_ref}\x1f{internal}")
        internal_keys.append(key)
        nodes.append(
            {
                "key": key,
                "label": label,
                "summary": _public_text(
                    field.get("help") or field.get("description"), limit=140
                ),
                "state": "必填" if bool(field.get("required")) else "可选",
            }
        )
        if not preview:
            for raw_candidate in _sequence(
                field.get("options") or field.get("candidates")
            )[:8]:
                if isinstance(raw_candidate, Mapping):
                    candidate = _mapping(raw_candidate)
                    candidate_label = _public_text(
                        candidate.get("label") or candidate.get("name"), limit=100
                    )
                    candidate_summary = _public_text(
                        candidate.get("description") or candidate.get("summary"),
                        limit=140,
                    )
                else:
                    candidate_label = _public_text(raw_candidate, limit=100)
                    candidate_summary = ""
                if candidate_label:
                    preview.append(
                        {
                            "label": candidate_label,
                            "summary": candidate_summary,
                            "state": "玩家可见候选",
                        }
                    )
    edges = [
        {
            "source": internal_keys[index],
            "target": internal_keys[index + 1],
            "label": "下一步",
        }
        for index in range(max(0, len(internal_keys) - 1))
    ]
    flow = {"nodes": nodes, "edges": edges} if nodes else {}
    selected = [nodes[0]] if nodes else []
    return flow, selected, preview


async def _designer_surface(context: SurfaceContext) -> SurfaceProjection:
    rows = await context.database.list_worlds(False)
    choices: list[dict[str, Any]] = []
    for item in rows or ():
        if not isinstance(item, Mapping):
            continue
        option = _project_world(context, item)
        option["value"] = option["key"]
        choices.append(option)
    world_ref = _resolve_world_context(context, required=False)
    if not world_ref:
        waiting_flows = [
            {
                "label": label,
                "summary": "选择世界后加载当前内容与真实检查结果。",
                "state": "等待选择",
            }
            for label in ("建卡流程", "世界内容", "规则与效果", "验证与发布")
        ]
        focus = {
            "label": "选择一个世界开始创作",
            "summary": "选择后才按需加载建卡检查与构筑矩阵。",
            "state": "等待选择",
        }
        return SurfaceProjection(
            data={
                "world_options": choices,
                "context": focus,
                "focus": focus,
                "flows": waiting_flows,
                "content_items": [],
                "problems": [],
                "reports": [],
            },
            summary={
                "label": "选择一个世界开始创作" if choices else "没有可编辑世界",
                "summary": (
                    "选择后按建卡、内容、规则、发布四条流程继续。"
                    if choices
                    else "先安装或创建一个世界。"
                ),
                "state": "等待选择" if choices else "空",
                "count": len(choices),
            },
            permissions={"can_view": True, "can_manage": True},
            empty=not choices,
        )

    from ..routes.designer_content import designer_coverage, designer_health

    world = _mapping(await context.database.get_world(world_ref))
    selected_name = _safe_label(world.get("name"), "世界名称缺失")
    problems: list[VisualProblem | Mapping[str, Any]] = []
    report: dict[str, Any] = {}
    coverage: dict[str, Any] = {}
    try:
        report = _route_body(
            await designer_health(
                context.principal,
                context.database,
                payload={"world_ref": world_ref},
            ),
            operation="检查世界建卡流程",
        )
    except Exception as exc:  # component-level partial result
        _status, problem = (
            _problem_from_adapter(exc)
            if isinstance(exc, WebRouteAdapterError)
            else problem_from_exception(exc)
        )
        problems.append(problem)
    try:
        coverage = _route_body(
            await designer_coverage(
                context.principal,
                context.database,
                payload={"world_ref": world_ref},
            ),
            operation="读取世界构筑矩阵",
        )
    except Exception as exc:  # component-level partial result
        _status, problem = (
            _problem_from_adapter(exc)
            if isinstance(exc, WebRouteAdapterError)
            else problem_from_exception(exc)
        )
        problems.append(problem)

    errors, warnings, suggestions = _designer_issue_counts(report)
    matrix = _sequence(coverage.get("matrix"))
    if not matrix:
        matrix = _sequence(report.get("matrix"))
    flows = [
        {
            "label": "建卡流程",
            "summary": (
                f"发现 {errors} 个阻断、{warnings} 个提醒。"
                if report
                else "建卡流程检查暂时不可用。"
            ),
            "state": "需要修复" if errors else "可继续" if report else "读取失败",
        },
        {
            "label": "世界内容",
            "summary": "选择场景、角色、物品、任务或阵营后再载入编辑区。",
            "state": "按需加载",
        },
        {
            "label": "规则与效果",
            "summary": (
                f"构筑矩阵包含 {len(matrix)} 个真实规则行。"
                if matrix
                else "没有足够数据绘制构筑矩阵。"
            ),
            "state": "可检查" if matrix else "数据不足",
        },
        {
            "label": "验证与发布",
            "summary": (
                f"还有 {errors + warnings + suggestions} 项检查结果需要审阅。"
                if report
                else "体检结果暂时不可用，不能据此发布。"
            ),
            "state": "阻塞" if errors else "待审阅" if report else "读取失败",
        },
    ]
    world_revision = _integer(world.get("revision"), 0)
    world_key = context.key("world", world_ref)
    adaptive_ui = public_ui_profile(world.get("ui_profile"))
    actor_template = _mapping(_mapping(world.get("rules")).get("actor"))
    field_options: list[dict[str, Any]] = []
    for field in _sequence(actor_template.get("fields")):
        value = _mapping(field)
        field_key = _text(value.get("key"), limit=200)
        label = _public_text(value.get("label"), limit=100)
        if field_key and label:
            field_options.append(
                {
                    "value": context.key(
                        "designer-field", f"{world_ref}\x1f{field_key}"
                    ),
                    "label": label,
                }
            )
    preset_options: list[dict[str, Any]] = []
    for set_key, raw_set in _mapping(actor_template.get("preset_sets")).items():
        values = (
            raw_set.values()
            if isinstance(raw_set, Mapping)
            else _sequence(raw_set)
        )
        for raw_preset in values:
            preset = _mapping(raw_preset)
            preset_id = _text(preset.get("id"), limit=200)
            label = _public_text(preset.get("label"), limit=100)
            if preset_id and label:
                preset_options.append(
                    {
                        "value": context.key(
                            "designer-preset",
                            f"{world_ref}\x1f{set_key}\x1f{preset_id}",
                        ),
                        "label": label,
                    }
                )
    primary_actions: list[dict[str, Any]] = []
    if world_revision > 0:
        primary_actions.append(
            _available_action(
                "E15",
                "author_job.create",
                "运行世界完整验证",
                target_kind="world",
                expected_revision=world_revision,
                description="登记发布前完整检查任务；后台会验证当前世界修订并保留可重放回执。",
            )
        )
        if field_options:
            primary_actions.append(
                _available_action(
                    "E01",
                    "designer.field.save",
                    "编辑角色卡字段",
                    target_kind="world",
                    expected_revision=world_revision,
                    description="选择现有字段并保存可读名称、帮助与类型；内部字段键不会进入表单。",
                    fields=[
                        {
                            "name": "field_key",
                            "type": "select",
                            "labelKey": "action.field.card_field",
                            "required": True,
                            "options": field_options,
                        },
                        {"name": "label", "type": "text", "labelKey": "action.field.name", "required": True},
                        {"name": "help", "type": "textarea", "labelKey": "action.field.description"},
                        {
                            "name": "type",
                            "type": "select",
                            "labelKey": "action.field.field_type",
                            "required": True,
                            "options": [
                                {"value": value, "label": label}
                                for value, label in (
                                    ("text", "单行文字"),
                                    ("textarea", "多行文字"),
                                    ("integer", "整数"),
                                    ("select", "单选"),
                                    ("preset_select", "预设单选"),
                                    ("multi_select", "多选"),
                                    ("boolean", "是/否"),
                                    ("derived", "自动计算"),
                                )
                            ],
                        },
                        {"name": "required", "type": "checkbox", "labelKey": "action.field.required"},
                    ],
                )
            )
        if preset_options:
            primary_actions.append(
                _available_action(
                    "E03",
                    "designer.preset.save",
                    "编辑角色预设",
                    target_kind="world",
                    expected_revision=world_revision,
                    description="选择现有预设并更新玩家可见名称与说明；内部预设键由服务端解析。",
                    fields=[
                        {
                            "name": "preset_key",
                            "type": "select",
                            "labelKey": "action.field.preset",
                            "required": True,
                            "options": preset_options,
                        },
                        {"name": "label", "type": "text", "labelKey": "action.field.name", "required": True},
                        {"name": "description", "type": "textarea", "labelKey": "action.field.description"},
                    ],
                )
            )
        primary_actions.append(
            _available_action(
                "E08",
                "resident_character.create",
                "新增常驻角色",
                target_kind="world",
                expected_revision=world_revision,
                description="用结构化表单建立 NPC 或常驻角色；服务端生成内部引用并保存防重复回执。",
                fields=[
                    {"name": "name", "type": "text", "labelKey": "action.field.name", "required": True},
                    {"name": "role", "type": "text", "labelKey": "action.field.role", "required": True, "value": "npc"},
                    {"name": "description", "type": "textarea", "labelKey": "action.field.description"},
                    {"name": "private_direction", "type": "textarea", "labelKey": "action.field.private_direction"},
                    {"name": "enabled", "type": "checkbox", "labelKey": "action.field.enabled", "value": True},
                ],
            )
        )
        primary_actions.append(
            _available_action(
                "E14",
                "designer.simulate",
                "运行确定性流程模拟",
                target_kind="world",
                expected_revision=world_revision,
                description="使用固定夹具检查转场、事件日志与运行态体积；不会保存世界或改动真实副本。",
                fields=[
                    {"name": "turns", "type": "number", "labelKey": "action.field.simulation_turns", "required": True, "value": 30},
                    {
                        "name": "party_size",
                        "type": "select",
                        "labelKey": "action.field.party_size",
                        "required": True,
                        "options": [{"value": size, "label": f"{size} 人"} for size in (1, 2, 4, 6, 8)],
                    },
                ],
            )
        )
    flows[0].update(
        {
            "key": world_key,
            "object_kind": "world",
            "revision": world_revision,
            "available_actions": primary_actions,
        }
    )
    content_items: list[dict[str, Any]] = []
    list_characters = getattr(context.database, "list_characters", None)
    resident_characters = (
        await _maybe_await(list_characters(world_ref))
        if callable(list_characters)
        else _sequence(world.get("characters"))
    )
    for raw_character in resident_characters or ():
        character = _mapping(raw_character)
        character_ref = _text(character.get("id"), limit=200)
        revision = _integer(character.get("revision"), 0)
        profile = _mapping(character.get("profile"))
        label = _safe_label(character.get("name"), "常驻角色名称缺失")
        enabled = bool(character.get("enabled"))
        actions: list[dict[str, Any]] = []
        if character_ref and revision > 0:
            actions.append(
                _available_action(
                    "E08",
                    "resident_character.update",
                    "编辑常驻角色",
                    target_kind="world-character",
                    expected_revision=revision,
                    description="保存名称、职责与公开说明；未出现在表单中的私密引导保持原值。",
                    fields=[
                        {"name": "name", "type": "text", "labelKey": "action.field.name", "required": True, "value": label},
                        {"name": "role", "type": "text", "labelKey": "action.field.role", "required": True, "value": _text(character.get("role"), limit=40)},
                        {"name": "description", "type": "textarea", "labelKey": "action.field.description", "value": _public_text(profile.get("description"), limit=4000)},
                        {"name": "enabled", "type": "checkbox", "labelKey": "action.field.enabled", "value": enabled},
                    ],
                )
            )
            if enabled:
                actions.append(
                    _available_action(
                        "C06",
                        "resident_character.retire",
                        "退役常驻角色",
                        target_kind="world-character",
                        expected_revision=revision,
                        description="停止在新内容中使用该角色，同时保留现有副本、关系与审计历史。",
                        fields=[
                            {"name": "reason", "type": "textarea", "labelKey": "action.field.reason", "required": True},
                            {"name": "acknowledge_retire", "type": "checkbox", "labelKey": "action.field.acknowledge_retire", "required": True},
                        ],
                    )
                )
        content_items.append(
            {
                "key": context.key(
                    "world-character", f"{world_ref}\x1f{character_ref}"
                ),
                "object_kind": "world-character",
                "label": label,
                "summary": _public_text(
                    profile.get("description"),
                    limit=180,
                    default="该角色尚未提供公开内容摘要。",
                ),
                "state": "可用于内容" if enabled else "已退役",
                "revision": revision,
                "available_actions": actions,
            }
        )
    flow, selected_node, candidate_preview = _designer_flow_projection(
        context, world_ref, report
    )
    matrix_projection = designer_matrix_projection(matrix)
    validation = [
        {
            "label": label,
            "summary": summary_text,
            "state": state,
        }
        for label, summary_text, state, count in (
            ("建卡阻断", f"当前检查发现 {errors} 个必须修复的问题。", "阻塞", errors),
            ("建卡提醒", f"当前检查发现 {warnings} 个需要审阅的提醒。", "提醒", warnings),
            ("改进建议", f"当前检查给出 {suggestions} 个可选改进。", "建议", suggestions),
        )
        if count
    ][:4]
    return SurfaceProjection(
        data={
            "items": flows,
            "flows": flows,
            "world_options": choices,
            "problems": validation,
            "flow": flow,
            "selected": selected_node,
            "preview": candidate_preview,
            "matrix": matrix_projection,
            "content_items": content_items,
            "world": {
                "key": world_key,
                "object_kind": "world",
                "label": selected_name,
                "state": "草稿检查",
                "revision": world_revision,
                "adaptive_ui": adaptive_ui,
                "ui_profile": adaptive_ui,
                "available_actions": primary_actions,
            },
            "context": {
                "key": world_key,
                "object_kind": "world",
                "label": selected_name,
                "state": "草稿检查",
                "revision": world_revision,
                "adaptive_ui": adaptive_ui,
                "ui_profile": adaptive_ui,
                "available_actions": primary_actions,
            },
        },
        summary={
            "label": selected_name,
            "summary": flows[0]["summary"],
            "state": flows[0]["state"],
            "count": len(flows),
        },
        revision=_integer(world.get("revision"), 0),
        updated_at=_text(world.get("updated_at"), limit=80),
        permissions={"can_view": True, "can_manage": True},
        problems=problems,
        state="partial" if problems else None,
    )
__all__ = [name for name in globals() if not name.startswith('__')]
