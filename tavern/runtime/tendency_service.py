"""Player-owned tendency application service and BOT projection."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from ..database_support import DatabaseNotFoundError, InvalidTransitionError
from .contracts import CommandError, CommandResult
from .request import RequestContext
from .turn_commit import build_tendency_visibility_plan, commit_plan


PAGE_SIZE = 5


class TendencyRepository(Protocol):
    async def player_tendency_view(
        self,
        session_id: str,
        user_id: str,
        *,
        include_revoked: bool = True,
    ) -> dict[str, Any]: ...

    async def set_tendency_evidence_visibility(
        self,
        session_id: str,
        user_id: str,
        number: int,
        *,
        restore: bool,
        operation_id: str = "",
    ) -> dict[str, Any]: ...


def _error(
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


def _page(items: list[Mapping[str, Any]], page: int) -> tuple[list[dict[str, Any]], int]:
    total_pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    if page < 1 or page > total_pages:
        raise IndexError(total_pages)
    start = (page - 1) * PAGE_SIZE
    return [dict(item) for item in items[start : start + PAGE_SIZE]], total_pages


def render_tendency_view(view: Mapping[str, Any], *, page: int = 1) -> str:
    observations = [
        dict(item)
        for item in view.get("observations", [])
        if isinstance(item, Mapping)
    ]
    active = [
        dict(item)
        for item in view.get("active_evidence", [])
        if isinstance(item, Mapping)
    ]
    revoked = [
        dict(item)
        for item in view.get("revoked_evidence", [])
        if isinstance(item, Mapping)
    ]
    current, total_pages = _page(active, page)
    lines = ["【我的倾向】"]
    if observations:
        for item in observations:
            lines.extend(
                [
                    f"〈{item.get('label') or '近期观察'}〉",
                    f"证据强度：{item.get('confidence_label') or '初步'}",
                    "",
                ]
            )
    else:
        lines.extend(
            [
                "证据仍不足，系统不会据此推断你的倾向。",
                "",
            ]
        )
    if current:
        lines.append("最近依据：")
        for item in current:
            lines.append(
                f"{int(item.get('number') or 0)}. "
                f"{item.get('summary') or '一次已提交的结构化选择'}"
            )
    else:
        lines.append("最近还没有可展示的结构化依据。")
    if total_pages > 1:
        lines.extend(
            [
                "",
                f"第 {page}/{total_pages} 页",
                f"查看下一页：/团 我的倾向 {min(total_pages, page + 1)}",
            ]
        )
    if revoked:
        lines.extend(
            [
                "",
                f"已忽略依据：{len(revoked)} 条",
                "恢复方式：/团 我的倾向 恢复 <序号>",
            ]
        )
    lines.extend(
        [
            "",
            str(
                view.get("privacy_notice")
                or "这些只是当前副本中的行为迹象，不会替你决定角色。"
            ),
            "忽略方式：/团 我的倾向 忽略 <序号>",
        ]
    )
    return "\n".join(lines)


class TendencyApplicationService:
    def __init__(self, repository: TendencyRepository) -> None:
        self.repository = repository

    async def execute(
        self,
        ctx: RequestContext,
        command: Any,
    ) -> CommandResult:
        if not ctx.session_id:
            return _error(
                ctx,
                code="command.session_required",
                operation="查看我的倾向",
                reason="当前群没有关联到可用副本。",
                next_command="/团 开启",
            )
        argument = str(getattr(command, "argument", "") or "").strip()
        parts = argument.split()
        action = "view"
        number = 0
        page = 1
        if parts and parts[0] in {"忽略", "恢复"}:
            action = "ignore" if parts[0] == "忽略" else "restore"
            if len(parts) != 2:
                return _error(
                    ctx,
                    code="tendency.evidence_not_found",
                    operation="调整倾向依据",
                    reason="请提供当前列表中的依据序号。",
                    next_command="/团 我的倾向",
                )
            try:
                number = int(parts[1])
            except ValueError:
                number = 0
            if number < 1:
                return _error(
                    ctx,
                    code="tendency.evidence_not_found",
                    operation="调整倾向依据",
                    reason="依据序号无效或已经失效。",
                    next_command="/团 我的倾向",
                )
        elif parts:
            if len(parts) != 1:
                return _error(
                    ctx,
                    code="command.invalid",
                    operation="查看我的倾向",
                    reason="页码格式不正确。",
                    next_command="/团 我的倾向",
                )
            try:
                page = int(parts[0])
            except ValueError:
                page = 0
            if page < 1:
                return _error(
                    ctx,
                    code="command.invalid",
                    operation="查看我的倾向",
                    reason="页码必须是大于 0 的数字。",
                    next_command="/团 我的倾向",
                )
        try:
            if action == "view":
                view = await self.repository.player_tendency_view(
                    ctx.session_id,
                    ctx.user_id,
                )
                try:
                    message = render_tendency_view(view, page=page)
                except IndexError as exc:
                    return _error(
                        ctx,
                        code="tendency.evidence_not_found",
                        operation="查看我的倾向",
                        reason=f"当前只有 {int(exc.args[0])} 页依据。",
                        next_command="/团 我的倾向",
                    )
                return CommandResult.reply(
                    message,
                    code=(
                        "tendency.insufficient_evidence"
                        if bool(view.get("insufficient"))
                        else "tendency.view.ready"
                    ),
                    data={"view": view, "page": page},
                )
            changed = await commit_plan(
                self.repository,
                build_tendency_visibility_plan(
                    operation_id=ctx.idempotency_key,
                    session_id=ctx.session_id,
                    user_id=ctx.user_id,
                    number=number,
                    restore=action == "restore",
                    actor_ref=ctx.user_id,
                    correlation_id=ctx.correlation_id,
                    expected_revision=ctx.expected_revision,
                ),
            )
            verb = "恢复" if action == "restore" else "忽略"
            replay = bool(changed.get("replayed"))
            message = (
                f"【倾向依据已{verb}】\n"
                f"依据：{changed.get('summary') or '一次已提交的结构化选择'}\n"
                "画像已按剩余有效依据重新计算。\n"
                + (
                    "该请求已处理过，本次返回第一次处理结果。"
                    if replay
                    else (
                        "如需撤销：/团 我的倾向 忽略 <序号>"
                        if action == "restore"
                        else "如需恢复：/团 我的倾向 恢复 <序号>"
                    )
                )
            )
            return CommandResult.reply(
                message,
                code=(
                    "command.replayed"
                    if replay
                    else f"tendency.evidence_{'restored' if action == 'restore' else 'ignored'}"
                ),
                status="replayed" if replay else "success",
                data={"change": changed},
            )
        except DatabaseNotFoundError as exc:
            code = (
                "tendency.evidence_not_found"
                if action != "view"
                else "command.not_found"
            )
            return _error(
                ctx,
                code=code,
                operation=(
                    "调整倾向依据" if action != "view" else "查看我的倾向"
                ),
                reason=str(exc),
                next_command="/团 我的倾向",
                status_code=404,
            )
        except InvalidTransitionError as exc:
            return _error(
                ctx,
                code=(
                    "tendency.restore_forbidden"
                    if action == "restore"
                    else "tendency.evidence_already_revoked"
                ),
                operation="调整倾向依据",
                reason=str(exc),
                next_command="/团 我的倾向",
            )


__all__ = [
    "PAGE_SIZE",
    "TendencyApplicationService",
    "TendencyRepository",
    "render_tendency_view",
]
