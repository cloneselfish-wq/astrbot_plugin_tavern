from __future__ import annotations

import importlib
import inspect
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

from ...protocol.errors import TwpPackageError
from ...storage import unlink_with_retry
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
from .world_packages import *
from .world_imports import *

@route_errors
async def github_scan(
    principal: Mapping[str, Any],
    *,
    payload: Mapping[str, Any] | None = None,
    github: Any = None,
    http_client: Any = None,
) -> dict[str, Any]:
    """扫描公开 GitHub 仓库中的世界包 ZIP（仓库文件树 + Release 附件）。"""
    require_login(principal)
    data = mapping(payload)
    url = text(data.get("url"))
    if not url:
        raise WebRouteError(
            400,
            "world.github.missing_url",
            "缺少 GitHub 仓库地址。",
            "请输入公开仓库地址后重试。",
        )
    github = _require_service(
        github,
        "authoring.service_unavailable",
        "GitHub 世界源不可用。",
        "请检查插件运行环境后重试。",
    )
    http_client = _require_service(
        http_client,
        "authoring.service_unavailable",
        "网络客户端不可用。",
        "请检查插件运行环境后重试。",
    )
    parsed = github.parse_repo_url(url)
    owner = text(parsed.get("owner"))
    repo = text(parsed.get("repo"))
    branch = text(data.get("branch")) or text(parsed.get("branch"))
    if not branch:
        info = await github.fetch_json(
            http_client,
            f"{github.GITHUB_API}/repos/{owner}/{repo}",
        )
        if not isinstance(info, dict):
            raise github.GithubWorldError("仓库信息无效")
        branch = github.default_branch(info)
    tree = await github.fetch_json(
        http_client,
        f"{github.GITHUB_API}/repos/{owner}/{repo}/git/trees/{branch}?recursive=1",
    )
    paths: list[str] = []
    if isinstance(tree, dict):
        tree_items = tree.get("tree")
        if isinstance(tree_items, list):
            paths = [
                str(item.get("path") or "")
                for item in tree_items
                if isinstance(item, dict)
            ]
        if tree.get("truncated"):
            raise github.GithubWorldError(
                "仓库文件较多，GitHub 只返回了部分内容；"
                "请在仓库 Release 发布 ZIP 后重试"
            )
    items: list[dict[str, Any]] = []
    for candidate in github.zip_candidates(paths):
        items.append(
            {
                "name": candidate["name"],
                "path": candidate["path"],
                "folder": candidate["folder"] or "/",
                "source": "repo",
                "url": github.raw_zip_url(
                    owner, repo, branch, candidate["path"]
                ),
            }
        )
    releases = await github.fetch_json(
        http_client,
        f"{github.GITHUB_API}/repos/{owner}/{repo}/releases",
    )
    if isinstance(releases, list):
        for release in releases:
            if not isinstance(release, dict):
                continue
            for asset in github.release_assets(release.get("assets")):
                items.append(
                    {
                        "name": asset["name"],
                        "path": asset["name"],
                        "folder": "Release 附件",
                        "source": "release",
                        "url": asset["url"],
                    }
                )
    return ok(
        {
            "owner": owner,
            "repo": repo,
            "branch": branch,
            "items": items,
        }
    )


@route_errors
async def github_import(
    principal: Mapping[str, Any],
    repos: Any,
    *,
    payload: Mapping[str, Any] | None = None,
    github: Any = None,
    http_client: Any = None,
    world_twp: Any = None,
    data_dir: Any = None,
    actor: str = "",
    publish: Callable[[Mapping[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """从白名单主机下载世界包 ZIP 并走 TWP 导入链路（管理员）。"""
    require_admin(principal)
    data = mapping(payload)
    url = text(data.get("url"))
    if not url:
        raise WebRouteError(
            400,
            "world.github.missing_url",
            "缺少下载地址。",
            "请输入世界包下载地址后重试。",
        )
    github = _require_service(
        github,
        "authoring.service_unavailable",
        "GitHub 世界源不可用。",
        "请检查插件运行环境后重试。",
    )
    http_client = _require_service(
        http_client,
        "authoring.service_unavailable",
        "网络客户端不可用。",
        "请检查插件运行环境后重试。",
    )
    world_twp = _require_service(
        world_twp,
        "authoring.service_unavailable",
        "世界包服务不可用。",
        "请检查插件运行状态后重试。",
    )
    if data_dir is None:
        raise WebRouteError(
            503,
            "authoring.service_unavailable",
            "缺少世界包导入目录。",
            "请检查插件数据目录配置后重试。",
        )
    raw = await github.fetch_zip(
        http_client,
        url,
        max_bytes=64 * 1024 * 1024,
    )
    temp_dir = Path(data_dir) / "imports" / "world-github"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_path = temp_dir / f"{uuid.uuid4().hex}.zip"
    temp_path.write_bytes(raw)
    try:
        return ok(
            await _install_twp_zip(
                repos,
                world_twp,
                temp_path,
                actor or actor_id(principal),
                publish,
            )
        )
    finally:
        unlink_with_retry(temp_path)


@route_errors
async def designer_health(
    principal: Mapping[str, Any],
    repos: Any,
    *,
    payload: Mapping[str, Any] | None = None,
    check: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    template: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """作者模板体检（只读）。"""
    require_login(principal)
    world = await _resolve_world(mapping(payload), repos)
    if check is None:
        check = _lazy("tavern.twp.validation.privacy", "check_template")
    if template is None:
        template = _lazy("tavern.lifecycle", "card_template")
    report = dict(check(world))
    report["actor_template"] = dict(template(world))
    return ok(report)


@route_errors
async def designer_coverage(
    principal: Mapping[str, Any],
    repos: Any,
    *,
    payload: Mapping[str, Any] | None = None,
    check: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """作者模板覆盖矩阵（只读）。"""
    require_login(principal)
    world = await _resolve_world(mapping(payload), repos)
    if check is None:
        check = _lazy("tavern.twp.validation.privacy", "coverage_matrix")
    return ok(check(world))


@route_errors
async def designer_candidates(
    principal: Mapping[str, Any],
    repos: Any,
    *,
    payload: Mapping[str, Any] | None = None,
    template: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    resolve: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """候选解析预览（只读）。"""
    require_login(principal)
    data = mapping(payload)
    world = await _resolve_world(data, repos)
    if template is None:
        template = _lazy("tavern.lifecycle", "card_template")
    if resolve is None:
        resolve = _lazy("tavern.twp.designer", "candidate_resolution")
    card = template(world)
    fields = data.get("fields", {})
    fields = dict(fields) if isinstance(fields, Mapping) else {}
    return ok(
        {
            "items": resolve(card, fields),
            "count": len(card.get("fields", [])),
        }
    )


__all__ = [name for name in globals() if not name.startswith('__')]


