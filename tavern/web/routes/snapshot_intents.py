"""RC8 snapshot intents and recoverable independent-archive trash.

The functions here are pure route services.  A host adapter must resolve opaque
surface keys to ``session_ref`` / ``snapshot_ref`` before calling them; no
function returns those internal references or raw snapshot state.

SQLite snapshot mutations are single-transaction CAS operations.  Independent
ZIP trash cannot share a transaction with SQLite, so C22 uses a durable
``reserved -> filesystem move/reconcile -> completed`` receipt.  A retry after
a crash converges only when exactly one planned path exists and its SHA-256 and
archive identity match the reserved plan.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import zipfile
from collections.abc import Mapping
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from ...database_support import DatabaseConflictError
from ...storage import (
    INSTANCE_FORMAT,
    file_sha256,
    replace_with_retry,
    safe_component,
)
from . import (
    WebRouteError,
    actor_id,
    ok,
    require_login,
    route_errors,
    text,
)
from .sessions import require_member


__all__ = [
    "archive_item_revision",
    "create_snapshot_intent",
    "delete_snapshot_intent",
    "reconcile_archive_trash_intent",
    "restore_snapshot_intent",
    "trash_archive_intent",
]

_MAX_JS_REVISION = (1 << 53) - 1


def _revision(value: Any, *, operation: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise WebRouteError(
            409,
            "snapshot.revision_required",
            f"{operation}失败：无法确认当前版本。",
            "系统没有修改数据；请刷新存档列表后重新确认。",
        ) from exc
    if parsed < 1 or parsed > _MAX_JS_REVISION:
        raise WebRouteError(
            409,
            "snapshot.revision_required",
            f"{operation}失败：当前版本已经失效。",
            "系统没有修改数据；请刷新存档列表后重新确认。",
        )
    return parsed


def _idempotency_key(value: Any, *, operation: str) -> str:
    key = text(value)
    if not key or len(key) > 200:
        raise WebRouteError(
            400,
            "snapshot.idempotency_required",
            f"{operation}失败：缺少有效的防重复凭据。",
            "系统没有修改数据；请保持当前窗口打开并重新提交。",
        )
    return key


async def _require_host(
    principal: Mapping[str, Any],
    database: Any,
    session_ref: str,
) -> None:
    require_login(principal)
    if not session_ref:
        raise WebRouteError(
            404,
            "snapshot.session_missing",
            "存档操作失败：所选副本已经不存在。",
            "请返回副本列表重新选择。",
        )
    role = await require_member(database, session_ref, principal)
    if role not in {"dm", "admin"}:
        raise WebRouteError(
            403,
            "snapshot.host_required",
            "存档操作失败：只有当前副本主持人或管理员可以执行。",
            "系统没有修改数据；请联系主持人处理。",
        )


@route_errors
async def create_snapshot_intent(
    principal: Mapping[str, Any],
    database: Any,
    *,
    session_ref: str,
    name: str,
    replace: bool,
    expected_revision: int,
    idempotency_key: str,
    expected_snapshot_revision: int | None = None,
    actor: str = "",
) -> dict[str, Any]:
    """E30/C31: create or explicitly replace a named DB snapshot."""

    session_ref = text(session_ref)
    await _require_host(principal, database, session_ref)
    clean_name = text(name)
    if not clean_name:
        raise WebRouteError(
            400,
            "snapshot.name_required",
            "创建存档失败：存档名称不能为空。",
            "请输入一个能辨认的存档名称后重试。",
        )
    session_revision = _revision(expected_revision, operation="创建存档")
    target_revision = (
        _revision(expected_snapshot_revision, operation="覆盖存档")
        if replace
        else None
    )
    result = await database.create_snapshot(
        session_ref,
        clean_name,
        actor or actor_id(principal),
        replace=bool(replace),
        expected_revision=session_revision,
        expected_snapshot_revision=target_revision,
        idempotency_key=_idempotency_key(
            idempotency_key,
            operation="创建存档",
        ),
    )
    return ok(result)


@route_errors
async def restore_snapshot_intent(
    principal: Mapping[str, Any],
    database: Any,
    *,
    session_ref: str,
    snapshot_ref: str,
    expected_revision: int,
    idempotency_key: str,
    actor: str = "",
) -> dict[str, Any]:
    """C20: restore a server-resolved snapshot and pause the session."""

    session_ref = text(session_ref)
    await _require_host(principal, database, session_ref)
    snapshot_ref = text(snapshot_ref)
    if not snapshot_ref:
        raise WebRouteError(
            400,
            "snapshot.target_required",
            "恢复存档失败：没有选择恢复目标。",
            "系统没有修改数据；请重新选择存档。",
        )
    result = await database.restore_snapshot(
        session_ref,
        snapshot_ref,
        actor or actor_id(principal),
        expected_revision=_revision(expected_revision, operation="恢复存档"),
        idempotency_key=_idempotency_key(
            idempotency_key,
            operation="恢复存档",
        ),
    )
    return ok(result)


@route_errors
async def delete_snapshot_intent(
    principal: Mapping[str, Any],
    database: Any,
    *,
    session_ref: str,
    snapshot_ref: str,
    expected_revision: int,
    idempotency_key: str,
    actor: str = "",
) -> dict[str, Any]:
    """C21: delete one manual DB snapshot after target CAS."""

    session_ref = text(session_ref)
    await _require_host(principal, database, session_ref)
    snapshot_ref = text(snapshot_ref)
    if not snapshot_ref:
        raise WebRouteError(
            400,
            "snapshot.target_required",
            "删除存档失败：没有选择删除目标。",
            "系统没有修改数据；请重新选择存档。",
        )
    result = await database.delete_snapshot(
        session_ref,
        snapshot_ref,
        actor or actor_id(principal),
        expected_revision=_revision(expected_revision, operation="删除存档"),
        idempotency_key=_idempotency_key(
            idempotency_key,
            operation="删除存档",
        ),
    )
    return ok(result)


def archive_item_revision(item: Mapping[str, Any]) -> int:
    """CAS revision for a listed independent archive (no path or stable id)."""

    canonical = json.dumps(
        {
            "filename": text(item.get("filename")),
            "kind": text(item.get("kind")),
            "size": int(item.get("size") or 0),
            "created_at": text(item.get("created_at")),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return int(hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:13], 16)


def _archive_filename(value: Any) -> str:
    filename = text(value)
    safe_name = Path(filename).name
    if (
        not filename
        or safe_name != filename
        or not safe_name.startswith("save_")
        or not safe_name.endswith(".zip")
    ):
        raise WebRouteError(
            400,
            "archive.filename_invalid",
            "删除独立存档失败：文件目标无效。",
            "系统没有移动任何文件；请刷新独立存档列表后重试。",
        )
    return safe_name


def _is_below(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _archive_identity(path: Path) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(path, "r") as archive:
            manifest = json.loads(
                archive.read("manifest.json").decode("utf-8")
            )
    except (
        KeyError,
        OSError,
        UnicodeDecodeError,
        ValueError,
        zipfile.BadZipFile,
    ) as exc:
        raise WebRouteError(
            400,
            "archive.verification_failed",
            "删除独立存档失败：文件无法通过完整性校验。",
            "系统没有移动该文件；请重新生成存档或联系管理员检查存储。",
        ) from exc
    if not isinstance(manifest, Mapping):
        raise WebRouteError(
            400,
            "archive.verification_failed",
            "删除独立存档失败：文件清单格式无效。",
            "系统没有移动该文件；请重新生成存档。",
        )
    archive_meta = manifest.get("archive")
    archive_meta = archive_meta if isinstance(archive_meta, Mapping) else {}
    session_meta = manifest.get("session")
    session_meta = session_meta if isinstance(session_meta, Mapping) else {}
    return {
        "format": text(manifest.get("format")),
        "format_version": int(manifest.get("format_version") or 0),
        "schema_version": int(manifest.get("schema_version") or 0),
        "session_id": text(session_meta.get("id")),
        "kind": text(archive_meta.get("kind")),
        "filename": text(archive_meta.get("filename")),
        "reason": text(archive_meta.get("reason")),
    }


def _validate_archive_identity(
    identity: Mapping[str, Any],
    *,
    session_ref: str,
    filename: str,
) -> None:
    if (
        text(identity.get("format")) != INSTANCE_FORMAT
        or text(identity.get("session_id")) != session_ref
        or text(identity.get("kind")) != "save"
        or text(identity.get("filename")) != filename
    ):
        raise WebRouteError(
            409,
            "archive.identity_conflict",
            "删除独立存档失败：文件不属于当前副本或目标已经变化。",
            "系统没有移动该文件；请刷新独立存档列表后重试。",
        )
    if "最终" in text(identity.get("reason")):
        raise WebRouteError(
            400,
            "archive.final_protected",
            "删除独立存档失败：最终保护存档不能移入回收目录。",
            "请保留最终存档；如需续作，请从该存档克隆新副本。",
        )


def _archive_paths(
    storage: Any,
    session_ref: str,
    filename: str,
    idempotency_key: str,
) -> tuple[Path, Path]:
    info = storage.storage_info(session_ref)
    relative = Path(text(info.get("relative_path")))
    if not relative.parts or relative.is_absolute() or ".." in relative.parts:
        raise WebRouteError(
            409,
            "archive.storage_scope_invalid",
            "删除独立存档失败：副本存储范围无法确认。",
            "系统没有移动该文件；请联系管理员检查副本存储索引。",
        )
    data_root = Path(storage.data_dir).resolve()
    story_root = (data_root / relative).resolve()
    saves_root = (story_root / "saves").resolve()
    trash_root = (story_root / "trash").resolve()
    if not _is_below(story_root, data_root):
        raise WebRouteError(
            409,
            "archive.storage_scope_invalid",
            "删除独立存档失败：副本存储范围无法确认。",
            "系统没有移动该文件；请联系管理员检查副本存储索引。",
        )
    source = (saves_root / filename).resolve()
    token = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:16]
    destination = (
        trash_root
        / (
            "deleted_"
            + safe_component(source.stem, "save", maximum=80)
            + "_"
            + token
            + ".zip"
        )
    ).resolve()
    if source.parent != saves_root or destination.parent != trash_root:
        raise WebRouteError(
            400,
            "archive.filename_invalid",
            "删除独立存档失败：文件目标无效。",
            "系统没有移动任何文件；请刷新独立存档列表后重试。",
        )
    return source, destination


def _build_archive_plan(
    storage: Any,
    *,
    session_ref: str,
    filename: str,
    expected_revision: int,
    idempotency_key: str,
) -> dict[str, Any]:
    item = next(
        (
            dict(value)
            for value in storage.list_archives(session_ref, kind="save")
            if isinstance(value, Mapping)
            and text(value.get("filename")) == filename
        ),
        None,
    )
    if item is None:
        raise WebRouteError(
            404,
            "archive.missing",
            "删除独立存档失败：文件已经不存在。",
            "系统没有移动其他文件；请刷新独立存档列表。",
        )
    if archive_item_revision(item) != expected_revision:
        raise WebRouteError(
            409,
            "archive.revision_conflict",
            "删除独立存档失败：文件已经发生变化。",
            "系统没有移动该文件；请刷新独立存档列表后重新确认。",
        )
    source, destination = _archive_paths(
        storage,
        session_ref,
        filename,
        idempotency_key,
    )
    if not source.is_file():
        raise WebRouteError(
            404,
            "archive.missing",
            "删除独立存档失败：文件已经不存在。",
            "系统没有移动其他文件；请刷新独立存档列表。",
        )
    if destination.exists():
        raise WebRouteError(
            409,
            "archive.destination_conflict",
            "删除独立存档失败：回收目标已被占用。",
            "系统没有覆盖任何文件；请使用新的操作重新确认。",
        )
    identity = _archive_identity(source)
    _validate_archive_identity(
        identity,
        session_ref=session_ref,
        filename=filename,
    )
    return {
        "source_path": str(source),
        "destination_path": str(destination),
        "fingerprint": file_sha256(source),
        "size": int(source.stat().st_size),
        "archive_identity": identity,
    }


def _verify_planned_archive(
    path: Path,
    plan: Mapping[str, Any],
    *,
    session_ref: str,
    filename: str,
) -> None:
    if not path.is_file():
        raise WebRouteError(
            503,
            "archive.reconcile_missing",
            "独立存档回收尚未完成：计划文件不可用。",
            "系统不会重复移动；请保留当前状态并重试对账。",
        )
    if (
        int(path.stat().st_size) != int(plan.get("size") or -1)
        or file_sha256(path) != text(plan.get("fingerprint"))
    ):
        raise WebRouteError(
            409,
            "archive.fingerprint_conflict",
            "独立存档回收停止：文件内容与预留计划不一致。",
            "系统没有覆盖或再次移动文件；请联系管理员核对存储。",
        )
    identity = _archive_identity(path)
    _validate_archive_identity(
        identity,
        session_ref=session_ref,
        filename=filename,
    )
    if identity != dict(plan.get("archive_identity") or {}):
        raise WebRouteError(
            409,
            "archive.identity_conflict",
            "独立存档回收停止：归档身份与预留计划不一致。",
            "系统没有覆盖或再次移动文件；请联系管理员核对存储。",
        )


def _reconcile_archive_plan(
    storage: Any,
    plan: Mapping[str, Any],
    *,
    session_ref: str,
    filename: str,
    idempotency_key: str,
) -> str:
    source = Path(text(plan.get("source_path"))).resolve()
    destination = Path(text(plan.get("destination_path"))).resolve()
    expected_source, expected_destination = _archive_paths(
        storage,
        session_ref,
        filename,
        idempotency_key,
    )
    if source != expected_source or destination != expected_destination:
        raise WebRouteError(
            409,
            "archive.plan_scope_conflict",
            "独立存档回收停止：预留路径不再属于当前副本存储范围。",
            "系统没有移动任何文件；请联系管理员核对存储迁移。",
        )
    lock = getattr(storage, "_lock", None)
    with lock if lock is not None else nullcontext():
        source_exists = source.is_file()
        destination_exists = destination.is_file()
        if source_exists and destination_exists:
            raise WebRouteError(
                409,
                "archive.reconcile_ambiguous",
                "独立存档回收停止：原位置和回收位置同时存在文件。",
                "系统不会覆盖或重复移动；请联系管理员核对两份文件。",
            )
        if not source_exists and not destination_exists:
            raise WebRouteError(
                503,
                "archive.reconcile_missing",
                "独立存档回收尚未完成：原位置和回收位置都没有计划文件。",
                "系统不会移动其他文件；请联系管理员恢复文件后重试对账。",
            )
        if destination_exists:
            _verify_planned_archive(
                destination,
                plan,
                session_ref=session_ref,
                filename=filename,
            )
            return "recovered"
        _verify_planned_archive(
            source,
            plan,
            session_ref=session_ref,
            filename=filename,
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        replace_with_retry(source, destination)
        _verify_planned_archive(
            destination,
            plan,
            session_ref=session_ref,
            filename=filename,
        )
        return "moved"


async def _mark_trash_failure(
    database: Any,
    *,
    session_ref: str,
    filename: str,
    actor: str,
    expected_revision: int,
    idempotency_key: str,
    error_code: str,
) -> None:
    try:
        await database.fail_archive_trash(
            session_ref,
            filename,
            actor,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            error_code=error_code,
        )
    except Exception:
        # The reserved plan is already durable.  A failed status update must not
        # hide the original error or trigger an unsafe compensating move.
        return


@route_errors
async def trash_archive_intent(
    principal: Mapping[str, Any],
    database: Any,
    *,
    session_ref: str,
    filename: str,
    expected_revision: int,
    idempotency_key: str,
    storage: Any | None = None,
    actor: str = "",
) -> dict[str, Any]:
    """C22: move one verified save ZIP through a recoverable two-phase receipt."""

    session_ref = text(session_ref)
    await _require_host(principal, database, session_ref)
    filename = _archive_filename(filename)
    revision = _revision(expected_revision, operation="删除独立存档")
    request_key = _idempotency_key(
        idempotency_key,
        operation="删除独立存档",
    )
    request_actor = actor or actor_id(principal)
    receipt = await database.prepare_archive_trash(
        session_ref,
        filename,
        request_actor,
        expected_revision=revision,
        idempotency_key=request_key,
        plan=None,
    )
    if receipt.get("status") == "completed":
        return ok(receipt["result"])
    archive_storage = storage or database.storage
    if receipt.get("status") == "unreserved":
        plan = await asyncio.to_thread(
            _build_archive_plan,
            archive_storage,
            session_ref=session_ref,
            filename=filename,
            expected_revision=revision,
            idempotency_key=request_key,
        )
        receipt = await database.prepare_archive_trash(
            session_ref,
            filename,
            request_actor,
            expected_revision=revision,
            idempotency_key=request_key,
            plan=plan,
        )
        if receipt.get("status") == "completed":
            return ok(receipt["result"])
    plan = receipt.get("plan")
    if not isinstance(plan, Mapping) or not plan:
        raise DatabaseConflictError("独立存档回收计划缺失")
    try:
        await asyncio.to_thread(
            _reconcile_archive_plan,
            archive_storage,
            plan,
            session_ref=session_ref,
            filename=filename,
            idempotency_key=request_key,
        )
    except WebRouteError as exc:
        await _mark_trash_failure(
            database,
            session_ref=session_ref,
            filename=filename,
            actor=request_actor,
            expected_revision=revision,
            idempotency_key=request_key,
            error_code=exc.code,
        )
        raise
    except OSError as exc:
        await _mark_trash_failure(
            database,
            session_ref=session_ref,
            filename=filename,
            actor=request_actor,
            expected_revision=revision,
            idempotency_key=request_key,
            error_code="archive.filesystem_retryable",
        )
        raise WebRouteError(
            503,
            "archive.filesystem_retryable",
            "删除独立存档暂未完成：服务器文件系统暂时不可用。",
            "预留计划已保存；请使用同一操作重试，系统会先对账再移动。",
        ) from exc
    try:
        result = await database.complete_archive_trash(
            session_ref,
            filename,
            request_actor,
            expected_revision=revision,
            idempotency_key=request_key,
        )
    except Exception as exc:
        await _mark_trash_failure(
            database,
            session_ref=session_ref,
            filename=filename,
            actor=request_actor,
            expected_revision=revision,
            idempotency_key=request_key,
            error_code="archive.receipt_retryable",
        )
        raise WebRouteError(
            503,
            "archive.receipt_retryable",
            "独立存档文件可能已经移入回收目录，但结果尚未确认。",
            "请使用同一操作重试；系统会按预留指纹对账，不会重复移动。",
        ) from exc
    return ok(result)


async def reconcile_archive_trash_intent(
    principal: Mapping[str, Any],
    database: Any,
    **kwargs: Any,
) -> dict[str, Any]:
    """Explicit recovery entry; identical to an idempotent C22 retry."""

    return await trash_archive_intent(principal, database, **kwargs)
