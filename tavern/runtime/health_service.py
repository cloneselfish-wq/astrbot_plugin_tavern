"""Router-backed health recovery actions and redacted diagnostic export."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..database_support import (
    DatabaseConflictError,
    DatabaseNotFoundError,
    InvalidTransitionError,
)
from ..backup_service import build_backup_archive
from ..storage import file_sha256, replace_with_retry, unlink_with_retry
from .contracts import CommandError, CommandResult
from .recovery_service import verify_backup_archive
from .request import RequestContext


HEALTH_ACTIONS = frozenset(
    {
        "health.outbox.retry",
        "health.lease.release_expired",
        "health.projection.rebuild",
        "health.world.verify",
        "health.author_job.retry",
        "health.backup.create",
        "health.diagnostics.export",
    }
)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


class HealthRecoveryService:
    """Only performs safe recovery; it never replays a domain command."""

    def __init__(self, repository: Any, data_dir: Path) -> None:
        self.repository = repository
        self.data_dir = Path(data_dir)

    def diagnostic_path(self, token: str) -> Path:
        token = str(token or "").strip().lower()
        if (
            len(token) != 24
            or any(character not in "0123456789abcdef" for character in token)
        ):
            raise ValueError("诊断下载凭据无效")
        path = (self.data_dir / "exports" / f"health_diagnostic_{token}.zip")
        resolved = path.resolve()
        export_root = (self.data_dir / "exports").resolve()
        if resolved.parent != export_root:
            raise ValueError("诊断下载路径无效")
        return resolved

    async def _export_diagnostic(
        self,
        ctx: RequestContext,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        token = hashlib.sha256(
            f"health-diagnostic:{ctx.idempotency_key}".encode("utf-8")
        ).hexdigest()[:24]
        path = self.diagnostic_path(token)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.is_file():
            temporary = path.with_name(f".{path.name}.tmp")
            report = {
                "format": "astrbot-tavern-health-diagnostic",
                "format_version": 1,
                "health": await self.repository.health_summary(),
                "scope": str(payload.get("scope") or "global"),
                "privacy": {
                    "identifiers": "omitted",
                    "private_messages": "omitted",
                    "credentials": "omitted",
                    "database_paths": "omitted",
                },
            }
            body = (
                json.dumps(
                    report,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")
            try:
                with zipfile.ZipFile(
                    temporary,
                    "w",
                    compression=zipfile.ZIP_DEFLATED,
                    compresslevel=6,
                ) as archive:
                    info = zipfile.ZipInfo("health.json")
                    info.date_time = (1980, 1, 1, 0, 0, 0)
                    info.external_attr = 0o100644 << 16
                    archive.writestr(info, body)
                replace_with_retry(temporary, path)
            finally:
                unlink_with_retry(temporary, suppress_errors=True)
        result = await self.repository.run_health_action(
            action="health.diagnostics.export",
            payload={
                "scope": str(payload.get("scope") or "global"),
                "download_token": token,
            },
            operation_id=ctx.idempotency_key,
            actor_id=f"web:{ctx.user_id}",
        )
        return {**result, "download_token": token}

    async def _create_backup(
        self,
        ctx: RequestContext,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        existing = await self.repository.health_action_receipt(
            action="health.backup.create",
            payload=payload,
            operation_id=ctx.idempotency_key,
        )
        if existing is not None:
            return existing
        label = "".join(
            character
            for character in str(payload.get("label") or "manual").strip()
            if character.isalnum() or character in {"-", "_"}
        )[:40] or "manual"
        path = await build_backup_archive(
            data_dir=self.data_dir,
            database=self.repository,
            export_dir=self.data_dir / "exports",
            prefix=f"backup_tavern_{label}",
        )
        try:
            bundle, checksums = await asyncio.to_thread(
                verify_backup_archive,
                path,
            )
            verified = {
                "summary": "新备份已创建并通过完整性校验。",
                "backup_name": path.name,
                "archive_sha256": file_sha256(path),
                "schema_version": int(bundle.get("schema_version") or 0),
                "verified_members": len(checksums),
                "next_action": "请保留这份新备份，并按恢复手册定期演练。",
            }
            stored = await self.repository.run_health_action(
                action="health.backup.create",
                payload={**dict(payload), "verified_result": verified},
                operation_id=ctx.idempotency_key,
                actor_id=f"web:{ctx.user_id}",
            )
            return {**verified, **stored}
        except Exception:
            # Validation failures must not leave a file that looks verified.
            unlink_with_retry(path, suppress_errors=True)
            raise

    async def execute(
        self,
        ctx: RequestContext,
        command: Any,
    ) -> CommandResult:
        action = str(getattr(command, "action", "") or "")
        payload = _mapping(getattr(command, "payload", {}))
        try:
            if action not in HEALTH_ACTIONS:
                raise ValueError("不支持的健康恢复动作")
            if action == "health.diagnostics.export":
                result = await self._export_diagnostic(ctx, payload)
            elif action == "health.backup.create":
                result = await self._create_backup(ctx, payload)
            else:
                result = await self.repository.run_health_action(
                    action=action,
                    payload=payload,
                    operation_id=ctx.idempotency_key,
                    actor_id=f"web:{ctx.user_id}",
                )
            return CommandResult(
                status="replayed" if result.get("replayed") else "success",
                code="health.action.completed",
                message=str(result.get("summary") or "健康恢复动作已完成。"),
                recovery=str(
                    result.get("next_action")
                    or "请刷新健康中心确认当前状态。"
                ),
                correlation_id=ctx.correlation_id,
                data={"result": result},
            )
        except PermissionError as exc:
            error = CommandError(
                code="command.permission_denied",
                operation="执行健康恢复",
                reason=str(exc),
                automatic_action="系统未修改任何数据。",
                next_command="请使用插件管理员账号重新登录。",
                correlation_id=ctx.correlation_id,
                status_code=403,
            )
        except DatabaseNotFoundError as exc:
            error = CommandError(
                code="health.target_not_found",
                operation="执行健康恢复",
                reason=str(exc),
                automatic_action="系统未修改任何数据。",
                next_command="请刷新健康中心后重新选择。",
                correlation_id=ctx.correlation_id,
                status_code=404,
            )
        except (DatabaseConflictError, InvalidTransitionError) as exc:
            error = CommandError(
                code="health.state_conflict",
                operation="执行健康恢复",
                reason=str(exc),
                automatic_action="系统保留当前数据和任务状态。",
                next_command="请刷新健康中心并确认失败原因。",
                correlation_id=ctx.correlation_id,
                status_code=409,
            )
        except (OSError, TypeError, ValueError, zipfile.BadZipFile) as exc:
            error = CommandError(
                code="health.action_failed",
                operation="执行健康恢复",
                reason=str(exc),
                automatic_action="系统未重放领域业务，并保留现有数据。",
                next_command="请导出诊断后按恢复手册处理。",
                correlation_id=ctx.correlation_id,
                status_code=400,
            )
        return CommandResult.failed(error)


__all__ = ["HEALTH_ACTIONS", "HealthRecoveryService"]
