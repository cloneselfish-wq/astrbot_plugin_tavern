from .common import *
from .sessions import *
from .characters import *
from .author_jobs import *
from .operations import *
from .settings import *

async def _resident_character_action(
    principal: Mapping[str, Any],
    database: Any,
    services: Any,
    *,
    intent: str,
    target_key: str,
    expected_revision: int,
    idempotency_key: str,
    body: Mapping[str, Any],
) -> dict[str, Any]:
    publish = _service(services, "publish")
    character_ref = ""
    if intent == "resident_character.create":
        world_ref = _resolved_target(
            principal, "designer", target_key, kind="world"
        )
    else:
        resolved = _resolved_target(
            principal,
            "designer",
            target_key,
            kind="world-character",
        )
        world_ref, character_ref = _split_scoped_target(
            resolved, parts=2, label="常驻角色"
        )
    if intent == "resident_character.retire":
        values = _checked_input(
            body, allowed=frozenset({"reason", "acknowledge_retire"})
        )
        if not flag(values.get("acknowledge_retire")):
            raise _route_error(
                400,
                "intent.character_retire_confirmation_required",
                "退役常驻角色前需要确认保留历史引用。",
                "请阅读影响说明并勾选确认后重新提交。",
            )
        result = await retire_resident_character_intent(
            principal,
            database,
            world_ref=world_ref,
            character_ref=character_ref,
            reason=text(values.get("reason")),
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            publish=publish,
        )
        action_id = "C06"
        state = "常驻角色已经退役，历史引用保持不变"
    else:
        values = _checked_input(
            body,
            allowed=frozenset(
                {"name", "role", "description", "private_direction", "enabled"}
            ),
        )
        result = await save_resident_character_intent(
            principal,
            database,
            world_ref=world_ref,
            character_ref=character_ref,
            values=values,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            publish=publish,
        )
        action_id = "E08"
        state = "常驻角色内容已经保存"
    output = _snapshot_route_body(
        result,
        fallback_code="intent.character_write_failed",
        fallback_message="常驻角色操作未能完成。",
        fallback_recovery="系统保留原内容；请刷新作者实验室后重试。",
    )
    return _safe_success(
        action_id=action_id,
        intent=intent,
        label=text(output.get("label"), "常驻角色"),
        state=state,
        revision=to_int(output.get("revision"), expected_revision),
        replayed=bool(output.get("replayed")),
    )


async def _github_import_action(
    principal: Mapping[str, Any],
    database: Any,
    services: Any,
    *,
    intent: str,
    target_key: str,
    expected_revision: int,
    idempotency_key: str,
    body: Mapping[str, Any],
) -> dict[str, Any]:
    require_admin(principal)
    if intent == "github.world.preview":
        source = _resolved_target(
            principal, "worlds", target_key, kind="github-source"
        )
        if source != "public-github-worlds" or expected_revision != 1:
            raise _route_error(
                409,
                "intent.github_source_changed",
                "GitHub 世界源状态已经变化。",
                "请刷新世界库后重新打开导入操作。",
            )
        values = _checked_input(
            body, allowed=frozenset({"repo_url", "branch"})
        )
        repo_url = text(values.get("repo_url"))
        if not repo_url:
            raise _route_error(
                400,
                "intent.github_url_required",
                "扫描 GitHub 世界包失败：缺少公开仓库地址。",
                "请输入 https://github.com/<owner>/<repo> 后重试。",
            )
        result = await preview_github_world_import(
            principal,
            database,
            repo_url=repo_url,
            branch=text(values.get("branch")),
            idempotency_key=idempotency_key,
            github=github_service,
            http_client=_service(services, "http_client"),
        )
        output = _snapshot_route_body(
            result,
            fallback_code="intent.github_preview_failed",
            fallback_message="扫描 GitHub 世界包未能完成。",
            fallback_recovery="系统没有安装任何内容；请检查仓库地址和网络后重试。",
        )
        candidates = [
            mapping(item)
            for item in output.get("candidates", [])
            if isinstance(item, Mapping)
        ]
        revision = to_int(output.get("revision"), 0) or 0
        operation_id = text(output.get("operation_id"))
        if not candidates or revision < 1 or not operation_id:
            raise _route_error(
                404,
                "intent.github_candidates_empty",
                "仓库中没有找到可导入的 ZIP 世界包。",
                "请在仓库或 Release 中发布完整 TWP ZIP 后重新扫描。",
            )
        continuation_key = issue_surface_key(
            principal, "worlds", "github-preview", operation_id
        )
        options = [
            {
                "value": index,
                "label": text(candidate.get("name"), f"世界包候选 {index + 1}"),
            }
            for index, candidate in enumerate(candidates[:100])
        ]
        return _safe_continuation(
            action_id="E28",
            label="GitHub 世界包预览",
            state=f"找到 {len(options)} 个可导入候选",
            revision=revision,
            intent="github.world.commit",
            target_key=continuation_key,
            target_kind="github-preview",
            description="选择一个候选后下载、完整体检并安装；失败会保留预览供重试。",
            fields=[
                {
                    "name": "candidate_index",
                    "type": "select",
                    "labelKey": "action.field.github_candidate",
                    "required": True,
                    "options": options,
                },
                {
                    "name": "acknowledge_install",
                    "type": "checkbox",
                    "labelKey": "action.field.acknowledge_install",
                    "required": True,
                },
            ],
            details=[
                {
                    "label": text(candidate.get("name"), f"候选 {index + 1}"),
                    "summary": (
                        "Release 附件"
                        if text(candidate.get("source")) == "release"
                        else "仓库文件"
                    ),
                    "state": "等待选择",
                }
                for index, candidate in enumerate(candidates[:12])
            ],
        )
    preview_ref = _resolved_target(
        principal, "worlds", target_key, kind="github-preview"
    )
    values = _checked_input(
        body, allowed=frozenset({"candidate_index", "acknowledge_install"})
    )
    if not flag(values.get("acknowledge_install")):
        raise _route_error(
            400,
            "intent.github_confirmation_required",
            "安装 GitHub 世界包前需要确认下载与导入影响。",
            "请确认候选名称后勾选安装确认。",
        )
    candidate_index = to_int(values.get("candidate_index"), -1)
    if candidate_index is None or candidate_index < 0:
        raise _route_error(
            400,
            "intent.github_candidate_required",
            "没有选择要安装的世界包候选。",
            "请从预览列表中选择一个候选。",
        )
    result = await commit_github_world_import(
        principal,
        database,
        preview_ref=preview_ref,
        candidate_index=candidate_index,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        http_client=_service(services, "http_client"),
        world_twp=_service(services, "world_twp"),
        data_dir=_service(services, "data_dir"),
        publish=_service(services, "publish"),
    )
    output = _snapshot_route_body(
        result,
        fallback_code="intent.github_commit_failed",
        fallback_message="安装 GitHub 世界包未能完成。",
        fallback_recovery="系统保留预览和可重试回执；请修复网络或包问题后重试。",
    )
    return _safe_success(
        action_id="E28",
        intent=intent,
        label=text(output.get("label"), "GitHub 世界包"),
        state="世界包已经通过体检并安装",
        revision=to_int(output.get("revision"), expected_revision),
        replayed=bool(output.get("replayed")),
    )


async def _session_pacing_preview(
    principal: Mapping[str, Any],
    database: Any,
    *,
    target_key: str,
    expected_revision: int,
    body: Mapping[str, Any],
) -> dict[str, Any]:
    session_id = _resolved_target(
        principal, "sessions", target_key, kind="session"
    )
    role = await require_member(database, session_id, principal)
    if role not in {"dm", "admin"}:
        raise _route_error(
            403,
            "intent.pacing_forbidden",
            "只有当前副本主持人或管理员可以调整剧情节奏。",
            "请联系主持人处理当前停滞。",
        )
    session = mapping(await database.get_session(session_id))
    if text(session.get("state")).lower() in {"finished", "closed"}:
        raise _route_error(
            409,
            "intent.pacing_readonly",
            "剧情节奏调整失败：当前副本已经归档并处于只读状态。",
            "系统没有改变世界；请返回活动副本，或从存档克隆新副本。",
        )
    values = _checked_input(body, allowed=frozenset({"action", "reason"}))
    action = text(values.get("action")).lower()
    if action not in _PACING_LABELS:
        raise _route_error(
            400,
            "intent.pacing_action_invalid",
            "没有选择可用的剧情节奏动作。",
            "请从页面列出的安全节奏动作中重新选择。",
        )
    reason = text(values.get("reason"), "console 剧情节奏预览")
    try:
        plan = mapping(
            await database.preview_story_pacing(
                session_id=session_id,
                action=action,
                expected_session_revision=expected_revision,
                actor_id=f"console:{actor_id(principal)}",
                source="console_webui",
                reason=reason,
            )
        )
    except DatabaseConflictError as exc:
        raise _route_error(
            409,
            "intent.pacing_preview_conflict",
            "剧情节奏预览失败：副本状态已经变化。",
            "系统没有改变世界；请刷新现场后重新预览。",
        ) from exc
    blockers = [mapping(item) for item in plan.get("blockers", []) if isinstance(item, Mapping)]
    if blockers:
        reasons = "；".join(
            text(item.get("message"), "存在未完成前置条件")
            for item in blockers[:3]
        )
        raise _route_error(
            409,
            "intent.pacing_blocked",
            f"剧情节奏预览发现阻塞：{reasons}",
            "系统没有改变世界；请先处理这些前置条件。",
        )
    plan_id = text(plan.get("plan_id"))
    preview_hash = text(plan.get("preview_hash"))
    plan_revision = to_int(plan.get("expected_session_revision"), expected_revision)
    if not plan_id or not preview_hash or plan_revision is None:
        raise _route_error(
            500,
            "intent.pacing_preview_invalid",
            "剧情节奏预览没有生成可确认计划。",
            "系统没有改变世界；请刷新后重试。",
        )
    internal = json.dumps(
        {
            "session_id": session_id,
            "plan_id": plan_id,
            "preview_hash": preview_hash,
            "revision": plan_revision,
            "reason": reason,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    continuation_key = issue_surface_key(
        principal,
        "sessions",
        "pacing",
        internal,
    )
    return _safe_continuation(
        action_id="C26",
        label=_PACING_LABELS[action],
        state="预览未改变世界",
        revision=plan_revision,
        intent="session.pacing.commit",
        target_key=continuation_key,
        target_kind="pacing",
        description="确认后会先建立不可覆盖快照，再原子提交这次节奏调整。",
        fields=[
            {
                "name": "acknowledge_pacing",
                "type": "checkbox",
                "labelKey": "action.field.acknowledge_pacing",
                "required": True,
            }
        ],
        details=[
            {
                "label": text(session.get("instance_name") or session.get("name"), "当前副本"),
                "summary": "当前剧情事实、玩家选择和已经结算的结果保持不变。",
                "state": "等待确认",
            }
        ],
    )


__all__ = [name for name in globals() if not name.startswith('__')]

