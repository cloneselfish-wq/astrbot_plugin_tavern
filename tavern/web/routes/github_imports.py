"""Staged, recoverable GitHub world import services for RC8."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

from ...github_worlds import GithubWorldError, fetch_zip, host_allowed_for_download
from ...protocol import TwpPackageError
from . import (
    WebRouteError,
    actor_id,
    mapping,
    ok,
    require_admin,
    route_errors,
    text,
)
from .designer_content import github_scan


def _service(value: Any, *, label: str) -> Any:
    if value is None:
        raise WebRouteError(
            503,
            "github.import.service_unavailable",
            f"{label}暂时不可用。",
            "系统保留已完成的预览；请检查插件运行状态后重试。",
        )
    return value


def _route_body(result: Mapping[str, Any], *, operation: str) -> dict[str, Any]:
    value = mapping(result)
    status = int(value.get("status") or 500)
    if status >= 400:
        error = mapping(value.get("error"))
        raise WebRouteError(
            status,
            text(error.get("code"), "github.import.source_failed"),
            text(error.get("reason") or error.get("message"), f"{operation}失败。"),
            text(
                error.get("next_command") or error.get("recovery"),
                "系统没有安装任何世界；请检查地址后重试。",
            ),
        )
    return mapping(value.get("body"))


@route_errors
async def preview_github_world_import(
    principal: Mapping[str, Any],
    repos: Any,
    *,
    repo_url: str,
    branch: str,
    idempotency_key: str,
    github: Any,
    http_client: Any,
) -> dict[str, Any]:
    """E28 stage 1: scan once, persist candidates, and replay safely."""

    require_admin(principal)
    actor = actor_id(principal)
    repo_url = text(repo_url)
    branch = text(branch)
    prepared = await repos.prepare_github_import_preview(
        actor,
        repo_url=repo_url,
        branch=branch,
        idempotency_key=idempotency_key,
    )
    if bool(prepared.get("replayed")):
        return ok(prepared)
    try:
        scanned = _route_body(
            await github_scan(
                principal,
                payload={"url": repo_url, "branch": branch},
                github=_service(github, label="GitHub 世界源"),
                http_client=_service(http_client, label="网络客户端"),
            ),
            operation="扫描 GitHub 世界包",
        )
        candidates = [
            dict(item)
            for item in scanned.get("items", [])
            if isinstance(item, Mapping)
            and host_allowed_for_download(text(item.get("url")))
        ]
        result = await repos.complete_github_import_preview(
            actor,
            repo_url=repo_url,
            branch=branch,
            resolved_branch=text(scanned.get("branch"), branch),
            idempotency_key=idempotency_key,
            owner_label=text(scanned.get("owner")),
            repository_label=text(scanned.get("repo")),
            candidates=candidates,
        )
        return ok(result)
    except Exception as exc:
        await repos.fail_github_import_preview(
            actor,
            idempotency_key=idempotency_key,
            error_code=text(getattr(exc, "code", None), type(exc).__name__),
        )
        raise


@route_errors
async def commit_github_world_import(
    principal: Mapping[str, Any],
    repos: Any,
    *,
    preview_ref: str,
    candidate_index: int,
    expected_revision: int,
    idempotency_key: str,
    http_client: Any,
    world_twp: Any,
    data_dir: Any,
    actor: str = "",
    publish: Callable[[Mapping[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """E28 stage 2: download, preflight/install, then durably commit receipt."""

    require_admin(principal)
    actor_value = actor or actor_id(principal)
    prepared = await repos.prepare_github_import_commit(
        actor_value,
        preview_operation_id=text(preview_ref),
        candidate_index=int(candidate_index),
        expected_revision=int(expected_revision),
        idempotency_key=idempotency_key,
    )
    if bool(prepared.get("replayed")):
        return ok(prepared)
    http_client = _service(http_client, label="网络客户端")
    world_twp = _service(world_twp, label="世界包服务")
    if data_dir is None:
        raise WebRouteError(
            503,
            "github.import.data_dir_unavailable",
            "世界包导入目录暂时不可用。",
            "系统保留已完成的预览；请检查插件数据目录后重试。",
        )
    candidate = mapping(prepared.get("candidate"))
    temp_dir = Path(data_dir) / "imports" / "world-github"
    temp_path = temp_dir / f"{uuid.uuid4().hex}.zip"
    try:
        raw = await fetch_zip(
            http_client,
            text(candidate.get("url")),
            max_bytes=64 * 1024 * 1024,
        )
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_path.write_bytes(raw)
        package_result = await world_twp.ensure_installed(temp_path, actor_value)
        report = mapping(package_result.get("report"))
        compiled = mapping(report.get("compiled_world"))
        installed = await repos.install_package_world(
            compiled,
            package=mapping(package_result.get("package")),
            actor_id=f"package:{actor_value}",
        )
        item = mapping(installed.get("item"))
        completed = await repos.complete_github_import_commit(
            actor_value,
            preview_operation_id=text(preview_ref),
            candidate_index=int(candidate_index),
            expected_revision=int(expected_revision),
            idempotency_key=idempotency_key,
            result={
                "label": text(item.get("name"), "已安装世界"),
                "revision": int(item.get("revision") or 0),
                "mode": text(installed.get("mode"), "installed"),
            },
        )
        if publish is not None and not bool(completed.get("replayed")):
            publish({"type": "world", "action": "github_import_completed"})
        return ok(completed)
    except Exception as exc:
        retryable = isinstance(exc, GithubWorldError) or not isinstance(
            exc, (ValueError, TwpPackageError, WebRouteError)
        )
        await repos.fail_github_import_commit(
            actor_value,
            idempotency_key=idempotency_key,
            error_code=text(getattr(exc, "code", None), type(exc).__name__),
            retryable=retryable,
        )
        raise
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


__all__ = [
    "commit_github_world_import",
    "preview_github_world_import",
]
