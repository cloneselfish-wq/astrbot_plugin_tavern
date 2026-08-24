"""GitHub 世界包导入：仓库解析、候选识别与受控下载（1.0.0-A5）。

只支持公开 GitHub 仓库；扫描走 api.github.com，下载只允许白名单主机
（raw.githubusercontent.com / github.com / objects.githubusercontent.com），
体积受 TWP 包上限约束；最终导入复用 ``worlds/twp`` 的体检与安装链路。
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

GITHUB_API = "https://api.github.com"
RAW_BASE = "https://raw.githubusercontent.com"
ALLOWED_DOWNLOAD_HOSTS = frozenset(
    {
        "raw.githubusercontent.com",
        "github.com",
        "objects.githubusercontent.com",
        "codeload.github.com",
    }
)

_REPO = re.compile(
    r"^https?://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/"
    r"(?P<repo>[A-Za-z0-9_.-]+)"
    r"(?:/tree/(?P<branch>[^/?#]+))?"
)
_RELEASES = re.compile(
    r"^https?://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/"
    r"(?P<repo>[A-Za-z0-9_.-]+)/releases"
)
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]+$")


class GithubWorldError(ValueError):
    """GitHub 世界包导入的用户可见错误。"""


def parse_repo_url(url: str) -> dict[str, str]:
    """解析 GitHub 仓库地址，返回 owner/repo/branch（branch 可选）。"""
    text = str(url or "").strip()
    match = _REPO.match(text)
    if not match:
        raise GithubWorldError(
            "仓库地址格式：https://github.com/<owner>/<repo>（可带 /tree/<分支>）"
        )
    branch = match.group("branch")
    if branch is not None and not _SAFE_SEGMENT.fullmatch(branch):
        raise GithubWorldError("分支名只允许字母、数字、点、下划线和连字符")
    return {
        "owner": match.group("owner"),
        "repo": match.group("repo"),
        "branch": branch or "",
    }


def is_release_url(url: str) -> bool:
    return bool(_RELEASES.match(str(url or "").strip()))


def host_allowed_for_download(url: str) -> bool:
    from urllib.parse import urlsplit

    parsed = urlsplit(str(url or ""))
    if parsed.scheme != "https":
        return False
    return (parsed.hostname or "").lower() in ALLOWED_DOWNLOAD_HOSTS


def default_branch(repo_info: Mapping[str, Any]) -> str:
    branch = str(repo_info.get("default_branch") or "")
    if not branch or not _SAFE_SEGMENT.fullmatch(branch):
        raise GithubWorldError("无法从仓库信息获取默认分支")
    return branch


def zip_candidates(paths: Sequence[str]) -> list[dict[str, str]]:
    """从 GitHub 文件树路径中筛选世界包 ZIP 候选。"""
    result: list[dict[str, str]] = []
    for raw in paths or []:
        path = str(raw or "")
        lowered = path.lower()
        if not lowered.endswith(".zip"):
            continue
        name = path.rsplit("/", 1)[-1]
        if not name:
            continue
        result.append(
            {
                "path": path,
                "name": name,
                "folder": path.rsplit("/", 1)[0] if "/" in path else "",
            }
        )
    # 稳定排序：根目录优先、名称升序
    result.sort(key=lambda item: (item["folder"] != "", item["name"].lower()))
    return result


async def fetch_json(client: Any, url: str) -> Any:
    """GET 一个 JSON 资源（client 需提供 async get(url)）。"""
    resp = await client.get(url)
    try:
        status = int(getattr(resp, "status", 500))
        if status >= 400:
            raise GithubWorldError(f"GitHub 请求失败：HTTP {status}")
        raw = await resp.read()
    finally:
        release = getattr(resp, "release", None)
        if callable(release):
            await release()
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise GithubWorldError(f"GitHub 返回内容无法解析：{exc}") from exc


async def fetch_zip(
    client: Any,
    url: str,
    *,
    max_bytes: int,
) -> bytes:
    """受控下载世界包 ZIP：仅白名单主机、带体积上限。"""
    if not host_allowed_for_download(url):
        raise GithubWorldError("下载地址主机不在白名单内")
    resp = await client.get(url)
    try:
        status = int(getattr(resp, "status", 500))
        if status >= 400:
            raise GithubWorldError(f"下载失败：HTTP {status}")
        headers = getattr(resp, "headers", None) or {}
        declared = headers.get("Content-Length")
        if declared and int(declared) > int(max_bytes):
            raise GithubWorldError(
                f"世界包超过体积上限（{int(max_bytes) // (1024 * 1024)} MB）"
            )
        raw = await resp.read()
    finally:
        release = getattr(resp, "release", None)
        if callable(release):
            await release()
    if len(raw) > int(max_bytes):
        raise GithubWorldError(
            f"世界包超过体积上限（{int(max_bytes) // (1024 * 1024)} MB）"
        )
    if not raw:
        raise GithubWorldError("下载内容为空")
    return raw


def raw_zip_url(owner: str, repo: str, branch: str, path: str) -> str:
    return f"{RAW_BASE}/{owner}/{repo}/{branch}/{path}"


def release_assets(assets: Any) -> list[dict[str, str]]:
    """从 GitHub Release 资源中筛选 zip 附件。"""
    result: list[dict[str, str]] = []
    if not isinstance(assets, Sequence) or isinstance(assets, (str, bytes)):
        return result
    for asset in assets:
        if not isinstance(asset, Mapping):
            continue
        name = str(asset.get("name") or "")
        url = str(asset.get("browser_download_url") or "")
        if name.lower().endswith(".zip") and url:
            result.append({"name": name, "url": url, "path": name})
    result.sort(key=lambda item: item["name"].lower())
    return result


__all__ = [
    "GithubWorldError",
    "default_branch",
    "fetch_json",
    "fetch_zip",
    "host_allowed_for_download",
    "is_release_url",
    "parse_repo_url",
    "raw_zip_url",
    "release_assets",
    "zip_candidates",
]
