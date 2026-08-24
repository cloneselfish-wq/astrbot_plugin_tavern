"""Pure Web routes for tendency, author jobs and health."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

from ...runtime.request import RequestContext
from . import (
    WebRouteError,
    actor_id,
    mapping,
    ok,
    require_admin,
    require_author,
    require_login,
    route_errors,
    text,
    to_int,
)
from .sessions import resolve_viewer_participant


logger = logging.getLogger(__name__)
_PRIVATE_DIAGNOSTIC_KEYS = frozenset({"correlation_id", "trace_id"})


def _without_private_diagnostics(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _without_private_diagnostics(nested)
            for key, nested in value.items()
            if str(key) not in _PRIVATE_DIAGNOSTIC_KEYS
        }
    if isinstance(value, list):
        return [_without_private_diagnostics(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_without_private_diagnostics(item) for item in value)
    return value


def _owner_user_id(participant: Mapping[str, Any] | None) -> str:
    if not isinstance(participant, Mapping):
        return ""
    return text(
        participant.get("group_user_id")
        or participant.get("user_id")
        or participant.get("private_user_id")
    )


def _request_context(
    principal: Mapping[str, Any],
    *,
    session_id: str = "",
    idempotency_key: str = "",
    expected_revision: int | None = None,
) -> RequestContext:
    username = actor_id(principal).removeprefix("web:")
    capabilities = {
        str(name)
        for name, enabled in mapping(principal.get("capabilities")).items()
        if enabled
    }
    if bool(principal.get("is_admin")):
        capabilities.add("admin")
    return RequestContext(
        correlation_id=idempotency_key,
        platform="web",
        user_id=username,
        session_id=session_id,
        roles=frozenset({"admin"} if bool(principal.get("is_admin")) else ()),
        capabilities=frozenset(capabilities),
        request_id=idempotency_key,
        idempotency_key=idempotency_key,
        expected_revision=expected_revision,
        metadata={"source": "web_console"},
    )


def _command_envelope(
    result: Any,
    *,
    success_status: int = 200,
) -> dict[str, Any]:
    if not bool(getattr(result, "ok", False)):
        error = getattr(result, "error", None)
        status = int(getattr(error, "status_code", 400) or 400)
        correlation_id = str(getattr(error, "correlation_id", "") or "")
        if correlation_id:
            log = logger.error if status >= 500 else logger.warning
            log(
                "Tavern Web command failed: correlation_id=%s status=%s code=%s",
                correlation_id,
                status,
                str(getattr(error, "code", "command.failed") or "command.failed"),
            )
        public = (
            error.public_dict()
            if error is not None
            else {
                "code": "command.failed",
                "operation": "执行页面操作",
                "reason": "操作未完成。",
                "automatic_action": "系统未修改任何数据。",
                "next_command": "请刷新页面后重试。",
            }
        )
        envelope = {
            "status": status,
            "error": _without_private_diagnostics(public),
        }
        safe_data = _without_private_diagnostics(
            dict(getattr(result, "data", {}) or {})
        )
        if safe_data:
            envelope["body"] = safe_data
        return envelope
    data = _without_private_diagnostics(
        dict(getattr(result, "data", {}) or {})
    )
    if getattr(result, "view", None) is not None:
        data["view"] = _without_private_diagnostics(dict(result.view))
    data["status"] = str(getattr(result, "status", "") or "success")
    data["message"] = str(getattr(result, "message", "") or "")
    data["recovery"] = str(getattr(result, "recovery", "") or "")
    return ok(data, status=success_status)


@route_errors
async def tendency_view(
    principal: Mapping[str, Any],
    repos: Any,
    *,
    session_id: str,
) -> dict[str, Any]:
    """Return only the signed-in player's semantic tendency projection."""

    require_login(principal)
    session_id = text(session_id)
    if not session_id:
        raise WebRouteError(
            400,
            "command.session_required",
            "缺少要查看的副本。",
            "请返回副本列表后重新进入。",
        )
    username = actor_id(principal).removeprefix("web:")
    participant = await resolve_viewer_participant(
        repos,
        session_id,
        username,
    )
    user_id = _owner_user_id(participant)
    if not user_id or user_id != username:
        raise WebRouteError(
            403,
            "tendency.owner_required",
            "只能查看本人在当前副本中的倾向依据。",
            "请使用已加入该副本的玩家账号登录。",
        )
    return ok(
        {
            "view": await repos.player_tendency_view(
                session_id,
                user_id,
            )
        }
    )


@route_errors
async def tendency_action(
    principal: Mapping[str, Any],
    repos: Any,
    router: Any,
    *,
    payload: Mapping[str, Any] | None = None,
    idempotency_key: str,
) -> dict[str, Any]:
    """Ignore or restore one current player-owned evidence number."""

    require_login(principal)
    data = mapping(payload)
    session_id = text(data.get("session_id"))
    action = text(data.get("action"))
    number = to_int(data.get("number"), 0) or 0
    if not session_id or action not in {"ignore", "restore"} or number < 1:
        raise WebRouteError(
            400,
            "request.invalid",
            "倾向依据操作缺少副本、动作或有效序号。",
            "请刷新“我的倾向”后重新选择。",
        )
    expected_revision = to_int(data.get("expected_revision"))
    if expected_revision is None:
        raise WebRouteError(
            409,
            "command.revision_required",
            "当前倾向页的状态版本已无法确认。",
            "请刷新“我的倾向”后重新选择。",
        )
    key = text(idempotency_key)
    if not key:
        raise WebRouteError(
            400,
            "command.idempotency_required",
            "请求缺少防重复凭据。",
            "请刷新页面后重新提交。",
        )
    username = actor_id(principal).removeprefix("web:")
    participant = await resolve_viewer_participant(
        repos,
        session_id,
        username,
    )
    user_id = _owner_user_id(participant)
    if not user_id or user_id != username:
        raise WebRouteError(
            403,
            "tendency.owner_required",
            "只能调整本人的倾向依据。",
            "请使用已加入该副本的玩家账号登录。",
        )
    result = await router.dispatch(
        _request_context(
            principal,
            session_id=session_id,
            idempotency_key=key,
            expected_revision=expected_revision,
        ),
        SimpleNamespace(
            action="tendency.evidence.action",
            payload={
                "owner_user_id": user_id,
                "number": number,
                "operation": action,
            },
        ),
    )
    envelope = _command_envelope(result)
    if envelope.get("status") == 200:
        body = mapping(envelope.get("body"))
        body["change"] = mapping(body.pop("data", None)).get("change", body.get("change"))
        envelope["body"] = body
    return envelope


@route_errors
async def author_jobs_view(
    principal: Mapping[str, Any],
    repos: Any,
    *,
    world_ref: str = "",
    limit: int = 100,
) -> dict[str, Any]:
    require_author(principal)
    revision = (
        await repos.author_jobs_revision()
        if hasattr(repos, "author_jobs_revision")
        else 0
    )
    return ok(
        {
            "jobs": await repos.list_author_jobs(
                world_ref=text(world_ref),
                limit=max(1, min(500, int(limit))),
            ),
            "revision": int(revision),
        }
    )


@route_errors
async def author_job_create(
    principal: Mapping[str, Any],
    router: Any,
    *,
    payload: Mapping[str, Any] | None = None,
    idempotency_key: str,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    require_author(principal)
    data = mapping(payload)
    key = text(idempotency_key)
    if not key:
        raise WebRouteError(
            400,
            "command.idempotency_required",
            "请求缺少防重复凭据。",
            "请刷新作者任务页后重新提交。",
        )
    result = await router.dispatch(
        _request_context(
            principal,
            idempotency_key=key,
            expected_revision=expected_revision,
        ),
        SimpleNamespace(action="author.job.create", payload=data),
    )
    success_status = (
        200 if str(getattr(result, "status", "")) == "replayed" else 202
    )
    envelope = _command_envelope(result, success_status=success_status)
    if envelope.get("status") in {200, 202}:
        body = mapping(envelope.get("body"))
        body["job"] = mapping(body.pop("data", None)).get("job", body.get("job"))
        envelope["body"] = body
    return envelope


@route_errors
async def author_job_action(
    principal: Mapping[str, Any],
    router: Any,
    *,
    payload: Mapping[str, Any] | None = None,
    idempotency_key: str,
) -> dict[str, Any]:
    require_author(principal)
    data = mapping(payload)
    job_ref = text(data.get("job_ref"))
    action = text(data.get("action"))
    if (
        not job_ref.startswith("public:author-job:")
        or action not in {"cancel", "retry"}
    ):
        raise WebRouteError(
            400,
            "author.job.invalid_action",
            "作者任务动作或任务引用无效。",
            "请刷新作者任务列表后重试。",
        )
    key = text(idempotency_key)
    if not key:
        raise WebRouteError(
            400,
            "command.idempotency_required",
            "请求缺少防重复凭据。",
            "请刷新作者任务页后重新提交。",
        )
    expected_revision = to_int(data.get("expected_revision"))
    if expected_revision is None:
        raise WebRouteError(
            409,
            "command.revision_required",
            "当前作者任务列表的状态版本已无法确认。",
            "请刷新作者任务列表后重新选择任务。",
        )
    result = await router.dispatch(
        _request_context(
            principal,
            idempotency_key=key,
            expected_revision=expected_revision,
        ),
        SimpleNamespace(
            action="author.job.action",
            payload={"job_ref": job_ref, "operation": action},
        ),
    )
    envelope = _command_envelope(result)
    if envelope.get("status") == 200:
        body = mapping(envelope.get("body"))
        body["job"] = mapping(body.pop("data", None)).get("job", body.get("job"))
        envelope["body"] = body
    return envelope


@route_errors
async def author_job_artifact(
    principal: Mapping[str, Any],
    repos: Any,
    *,
    job_ref: str,
    artifact_type: str,
) -> dict[str, Any]:
    require_author(principal)
    if (
        not text(job_ref).startswith("public:author-job:")
        or not text(artifact_type)
    ):
        raise WebRouteError(
            400,
            "author.artifact.invalid_request",
            "缺少有效的任务引用或报告类型。",
            "请从作者任务详情重新下载。",
        )
    return ok(
        {
            "artifact": await repos.author_job_artifact(
                text(job_ref),
                text(artifact_type),
            )
        }
    )


@route_errors
async def health_view(
    principal: Mapping[str, Any],
    repos: Any,
) -> dict[str, Any]:
    require_admin(principal)
    return ok({"health": await repos.health_summary()})


@route_errors
async def health_action(
    principal: Mapping[str, Any],
    router: Any,
    *,
    payload: Mapping[str, Any] | None = None,
    idempotency_key: str,
) -> dict[str, Any]:
    require_admin(principal)
    data = mapping(payload)
    action = text(data.get("action"))
    key = text(idempotency_key)
    if not action:
        raise WebRouteError(
            400,
            "health.action_required",
            "缺少要执行的健康恢复动作。",
            "请刷新健康中心后重新选择。",
        )
    if not key:
        raise WebRouteError(
            400,
            "command.idempotency_required",
            "请求缺少防重复凭据。",
            "请刷新健康中心后重新提交。",
        )
    result = await router.dispatch(
        _request_context(principal, idempotency_key=key),
        SimpleNamespace(action=action, payload=data),
    )
    envelope = _command_envelope(result)
    if envelope.get("status") == 200:
        body = mapping(envelope.get("body"))
        body["result"] = mapping(body.pop("data", None)).get(
            "result",
            body.get("result"),
        )
        envelope["body"] = body
    return envelope


__all__ = [
    "author_job_action",
    "author_job_artifact",
    "author_job_create",
    "author_jobs_view",
    "health_action",
    "health_view",
    "tendency_action",
    "tendency_view",
]
