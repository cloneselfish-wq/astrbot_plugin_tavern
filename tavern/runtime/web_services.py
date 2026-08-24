"""Router-backed Web write services.

Pure Web routes resolve identity and validate transport input.  Every write is
then dispatched through :class:`ApplicationRouter` to this service, which
returns the same ``CommandResult`` contract used by BOT commands.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..database_support import (
    DatabaseConflictError,
    DatabaseNotFoundError,
    InvalidTransitionError,
)
from .contracts import CommandError, CommandResult
from .request import RequestContext
from .turn_commit import (
    build_author_job_action_plan,
    build_author_job_create_plan,
    build_tendency_visibility_plan,
    commit_plan,
)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _failure(
    ctx: RequestContext,
    *,
    code: str,
    operation: str,
    reason: str,
    next_command: str,
    status_code: int = 400,
) -> CommandResult:
    return CommandResult.failed(
        CommandError(
            code=code,
            operation=operation,
            reason=reason,
            automatic_action="系统未修改任何数据。",
            next_command=next_command,
            correlation_id=ctx.correlation_id,
            status_code=status_code,
        )
    )


class WebApplicationService:
    """Execute tendency and author writes behind one Router boundary."""

    def __init__(self, repository: Any) -> None:
        self.repository = repository

    async def execute(
        self,
        ctx: RequestContext,
        command: Any,
    ) -> CommandResult:
        action = str(getattr(command, "action", "") or "")
        payload = _mapping(getattr(command, "payload", {}))
        try:
            if action == "tendency.evidence.action":
                restore = str(payload.get("operation") or "") == "restore"
                change = await commit_plan(
                    self.repository,
                    build_tendency_visibility_plan(
                        operation_id=ctx.idempotency_key,
                        session_id=ctx.session_id,
                        user_id=str(
                            payload.get("owner_user_id") or ctx.user_id
                        ),
                        number=int(payload.get("number") or 0),
                        restore=restore,
                        actor_ref=ctx.user_id,
                        correlation_id=ctx.correlation_id,
                        expected_revision=ctx.expected_revision,
                    ),
                )
                view = await self.repository.player_tendency_view(
                    ctx.session_id,
                    str(payload.get("owner_user_id") or ctx.user_id),
                )
                return CommandResult(
                    status="replayed" if change.get("replayed") else "success",
                    code="tendency.evidence.updated",
                    message=(
                        "已恢复这条倾向依据。"
                        if restore
                        else "已忽略这条倾向依据。"
                    ),
                    recovery="如需调整，可在当前列表继续操作。",
                    correlation_id=ctx.correlation_id,
                    data={"change": change},
                    view=view,
                )
            if action == "author.job.create":
                request_payload = _mapping(payload.get("request"))
                if not request_payload:
                    request_payload = {
                        key: value
                        for key, value in payload.items()
                        if key
                        not in {
                            "job_type",
                            "world_ref",
                            "max_attempts",
                            "idempotency_key",
                            "request_id",
                        }
                    }
                receipt_request = {
                    "job_type": str(payload.get("job_type") or ""),
                    "world_ref": str(payload.get("world_ref") or ""),
                    "request": request_payload,
                    "max_attempts": int(payload.get("max_attempts") or 3),
                }
                public_job = await commit_plan(
                    self.repository,
                    build_author_job_create_plan(
                        operation_id=ctx.idempotency_key,
                        job_type=receipt_request["job_type"],
                        world_ref=receipt_request["world_ref"],
                        request_payload=request_payload,
                        actor_ref=f"web:{ctx.user_id}",
                        max_attempts=receipt_request["max_attempts"],
                        # Semantic world-scoped creation carries the world's
                        # revision into the same transaction as the job insert.
                        # Legacy callers may still omit it because the author-job
                        # list revision is independent cache metadata.
                        expected_revision=ctx.expected_revision,
                    ),
                )
                return CommandResult(
                    status=(
                        "replayed"
                        if public_job.get("replayed")
                        else "accepted"
                    ),
                    code="author.job.accepted",
                    message=(
                        "已返回原作者任务。"
                        if public_job.get("replayed")
                        else "作者任务已进入队列。"
                    ),
                    recovery="可在“作者任务”查看进度、取消或重试。",
                    correlation_id=ctx.correlation_id,
                    data={"job": public_job},
                )
            if action == "author.job.action":
                job = await commit_plan(
                    self.repository,
                    build_author_job_action_plan(
                        operation_id=ctx.idempotency_key,
                        job_ref=str(payload.get("job_ref") or ""),
                        action=str(payload.get("operation") or ""),
                        actor_ref=f"web:{ctx.user_id}",
                        expected_revision=ctx.expected_revision,
                    ),
                )
                return CommandResult(
                    status="replayed" if job.get("replayed") else "success",
                    code="author.job.updated",
                    message="作者任务状态已更新。",
                    recovery="请刷新任务列表确认当前状态。",
                    correlation_id=ctx.correlation_id,
                    data={"job": job},
                )
            return _failure(
                ctx,
                code="command.unknown",
                operation="执行页面操作",
                reason="当前页面动作不存在。",
                next_command="请刷新页面后重试。",
            )
        except DatabaseNotFoundError as exc:
            return _failure(
                ctx,
                code="resource.not_found",
                operation="执行页面操作",
                reason=str(exc),
                next_command="请刷新列表后重新选择。",
                status_code=404,
            )
        except (DatabaseConflictError, InvalidTransitionError) as exc:
            failure = _failure(
                ctx,
                code="command.state_conflict",
                operation="执行页面操作",
                reason=str(exc),
                next_command="请刷新当前状态后重试。",
                status_code=409,
            )
            if action == "author.job.action":
                job_ref = str(payload.get("job_ref") or "")
                try:
                    latest = await self.repository.author_job_public_view_by_ref(
                        job_ref
                    )
                except Exception:
                    latest = {}
                if latest:
                    failure.data = {"job": latest}
            return failure
        except (TypeError, ValueError) as exc:
            return _failure(
                ctx,
                code="request.invalid",
                operation="执行页面操作",
                reason=str(exc),
                next_command="请检查所选项目后重新提交。",
            )


__all__ = ["WebApplicationService"]
