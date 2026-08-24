from __future__ import annotations

import asyncio
import inspect
import json
import hashlib
import os
import re
import shutil
import sqlite3
import stat
import time
import uuid
import zipfile
from datetime import datetime, timezone
from collections import OrderedDict
from contextlib import closing
from pathlib import Path, PurePosixPath
from collections.abc import Mapping
from typing import Any

from astrbot.api.web import (
    PluginUploadFile,
    error_response,
    file_response,
    json_response,
    request,
    stream_response,
)

from .config import TavernConfig, merge_config_payload
from .builtin_worlds import (
    merge_builtin_world_statuses,
    project_world_catalog,
)
from .constants import (
    CHARACTER_CARD_SCHEMA_VERSION,
    DATABASE_SCHEMA_VERSION,
    DEFAULT_CHARACTER_CARD_CONTENT_VERSION,
    DEFAULT_WORLD_CONTENT_VERSION,
    DEFAULT_WORLD_DISPLAY_VERSION,
    PLUGIN_NAME,
    PLUGIN_VERSION,
    WORLD_PROTOCOL_VERSION,
    SESSION_CLOSED,
    SESSION_FINISHED,
    SESSION_MAINTENANCE,
    SESSION_PAUSED,
    SESSION_PREPARING,
    SESSION_RUNNING,
)
from .database import (
    DatabaseConflictError,
    DatabaseNotFoundError,
    InvalidTransitionError,
    TavernDatabase,
)
from .database_support import validate_slug
from .events import EventBroker
from .lifecycle import normalize_time_rules
from .diagnostics import build_diagnostic_report
from .emergency import EmergencyService
from .errors import PolicyRejection, report_failure
from .projections.dashboard import (
    dashboard_sessions as build_dashboard_sessions,
    session_timers as build_session_timers,
)
from .projections.session_dashboard import enrich_session_display_labels
from .projections.session_details import session_timeline as build_session_timeline
from .projections.session_timeline import session_dashboard as build_session_dashboard
from .operations import recovery_summary
from .elemental import (
    parse as parse_elemental,
    resolve as resolve_elemental,
    table as elemental_table,
)
from .world_contract import RESOLUTION_MODES, WORLD_SCHEMA_VERSION
from .operation_engine import OPERATION_TYPES, PERSISTENCE_SCOPES
from .api.hooks import SUPPORTED_EVENTS as HOOK_EVENTS
from .api.registry import ExtensionRegistry
from .rule_runtime import RuleRuntime
from .entity_registry import EntityRegistry
from .backup_service import build_backup_archive
from .storage import (
    file_sha256,
    next_timestamped_path,
    replace_with_retry,
    unlink_with_retry,
)
from .platform_delivery import capability_matrix
from .delivery.service import DeliveryOutcome, DeliveryService
from .delivery.target import DeliveryTarget
from .chat_experience import normalize_chat_experience
from .presets import (
    PresetLibraryContractError,
    normalize_preset_libraries,
)
from .projections.character import project_actor_view
from .projections.delivery import project_delivery_status_items
from .projections.world import project_npc_view, project_world_state_view
from .session_events import (
    has_structural_event,
    project_session_event,
    summarize_affected_modules,
)
from .web.routes import error_from_exception
from .web.routes.assets import (
    assets_view as route_assets_view,
    economy_adjust as route_economy_adjust,
    economy_migrate_world as route_economy_migrate_world,
    economy_set_enabled as route_economy_set_enabled,
    economy_summary as route_economy_summary,
    economy_transactions as route_economy_transactions,
)
from .web.routes.characters import (
    character_detail_view as route_character_detail_view,
    character_list_view as route_character_list_view,
    supplement_offers_view as route_supplement_offers_view,
)
from .web.routes.narrative_control import (
    narrative_control_view as route_narrative_control_view,
)
from .web.routes.operations import (
    deliveries_act as route_deliveries_act,
    deliveries_view as route_deliveries_view,
    diagnostics_view as route_diagnostics_view,
    operation_cancel as route_operation_cancel,
    operations_view as route_operations_view,
)
from .web.routes.tendencies import (
    author_job_action as route_author_job_action,
    author_job_artifact as route_author_job_artifact,
    author_job_create as route_author_job_create,
    author_jobs_view as route_author_jobs_view,
    health_action as route_health_action,
    health_view as route_health_view,
    tendency_action as route_tendency_action,
    tendency_view as route_tendency_view,
)
from .runtime.command_router import ApplicationRouter, CommandSpec
from .runtime.health_service import HEALTH_ACTIONS, HealthRecoveryService
from .runtime.web_services import WebApplicationService
from .runtime.recovery_service import BackupRecoveryService
from .provider_health_service import ProviderHealthService
from .web.routes.sessions import (
    resolve_viewer_participant,
    session_detail_view as semantic_session_detail_view,
    session_shell_view as semantic_session_shell_view,
)
from .web.routes.actor_fate import (
    actor_fate_consent_view as route_actor_fate_consent_view,
)
from .web.routes.world_state import world_state_view as route_world_state_view
from .web.routes.session_generation import session_generation_view as route_visual_session_generation
from .web.routes.session_party import session_summary_view as route_visual_session_summary
from .web.routes.session_world import (
    session_history_view as route_visual_session_history,
    session_party_view as route_visual_session_party,
    session_world_visuals_view as route_visual_session_world,
)
from .web.surfaces.system import route_surface_view, surface_error_response
from .web.errors import WebApiError
from .modules import ModuleDependencyError, PluginModuleManager
from .protocol import TwpPackageError, TwpPackageService
from .remote_panel import (
    default_credentials,
    hash_password,
    load_credentials,
    save_credentials,
)
from .github_worlds import (
    GITHUB_API,
    GithubWorldError,
    default_branch,
    fetch_json,
    fetch_zip,
    parse_repo_url,
    raw_zip_url,
    release_assets,
    zip_candidates,
)


_BACKUP_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MAX_BACKUP_UNCOMPRESSED = 4 * 1024 * 1024 * 1024
def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _preset_issue_set_id(problem: Mapping[str, Any]) -> str:
    """从契约问题 path 中解析预设库稳定 id（无则返回空串）。"""
    path = str(problem.get("path") or "")
    marker = "preset_libraries."
    if marker in path:
        rest = path.split(marker, 1)[1]
        candidate = rest.split(".", 1)[0]
        if candidate:
            return candidate
    return ""


def _content_version_key(value: Any) -> tuple[int, ...] | None:
    text = str(value or "").strip().lstrip("vV")
    match = re.fullmatch(r"\d+(?:\.\d+){0,3}", text)
    if not match:
        return None
    return tuple(int(part) for part in text.split("."))


def _safe_backup_member(name: str) -> PurePosixPath:
    if not name or "\\" in name:
        raise ValueError("ZIP 备份含非法文件路径")
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("ZIP 备份含非法文件路径")
    return path


def _backup_checksums(archive: zipfile.ZipFile) -> dict[str, str]:
    try:
        checksum_info = archive.getinfo("checksum.sha256")
    except KeyError as exc:
        raise ValueError("ZIP 备份缺少 checksum.sha256") from exc
    if checksum_info.file_size > 16 * 1024 * 1024:
        raise ValueError("ZIP 备份校验清单过大")
    try:
        text = archive.read(checksum_info).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("ZIP 备份校验清单编码错误") from exc
    checksums: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("  ", 1)
        if len(parts) != 2:
            raise ValueError("ZIP 备份校验清单格式错误")
        digest, name = parts[0].strip().lower(), parts[1].strip()
        _safe_backup_member(name)
        if not _BACKUP_HASH_PATTERN.fullmatch(digest) or name in checksums:
            raise ValueError("ZIP 备份校验清单格式错误")
        checksums[name] = digest
    return checksums


def _verify_backup_archive(archive: zipfile.ZipFile) -> dict[str, str]:
    infos = archive.infolist()
    names: set[str] = set()
    total_size = 0
    for info in infos:
        path = _safe_backup_member(info.filename.rstrip("/"))
        name = path.as_posix()
        if name in names:
            raise ValueError("ZIP 备份含重复文件名")
        names.add(name)
        if info.flag_bits & 0x1:
            raise ValueError("不支持加密 ZIP 备份")
        mode = (info.external_attr >> 16) & 0xFFFF
        if mode and stat.S_ISLNK(mode):
            raise ValueError("ZIP 备份不能包含符号链接")
        if not info.is_dir():
            total_size += int(info.file_size)
    if total_size > _MAX_BACKUP_UNCOMPRESSED:
        raise ValueError("ZIP 解压后的总大小不能超过 4 GiB")
    checksums = _backup_checksums(archive)
    payload_names = {
        info.filename
        for info in infos
        if not info.is_dir() and info.filename != "checksum.sha256"
    }
    if payload_names != set(checksums):
        raise ValueError("ZIP 备份文件与校验清单不一致")
    for info in infos:
        if info.is_dir() or info.filename == "checksum.sha256":
            continue
        digest = hashlib.sha256()
        with archive.open(info, "r") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != checksums[info.filename]:
            raise ValueError(f"ZIP 备份文件校验失败：{info.filename}")
    return checksums


def _stage_managed_files(
    archive: zipfile.ZipFile,
    stage_dir: Path,
) -> list[tuple[Path, PurePosixPath]]:
    staged: list[tuple[Path, PurePosixPath]] = []
    for info in archive.infolist():
        if info.is_dir():
            continue
        relative = _safe_backup_member(info.filename)
        if not relative.parts or relative.parts[0] not in {
            "groups", "world_packages_twp", "plugin_modules.json"
        }:
            continue
        target = stage_dir.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info, "r") as source, target.open("wb") as output:
            shutil.copyfileobj(source, output, length=1024 * 1024)
        staged.append((target, relative))
    return staged


def _collision_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(
            f"{path.stem}_{index:02d}{path.suffix}"
        )
        if not candidate.exists():
            return candidate
    raise RuntimeError("同名独立存档过多")


def _merge_package_index(destination: Path, staged: Path) -> None:
    """Merge package catalogs insert-only; live entries win conflicts."""
    current = json.loads(destination.read_text(encoding="utf-8"))
    incoming = json.loads(staged.read_text(encoding="utf-8"))
    current_packages = current.get("packages", {})
    incoming_packages = incoming.get("packages", {})
    if not isinstance(current_packages, dict) or not isinstance(incoming_packages, dict):
        raise ValueError("世界包索引格式无效")
    value = {
        **incoming,
        **current,
        "format": current.get("format", incoming.get("format", 1)),
        "packages": {**incoming_packages, **current_packages},
    }
    temporary = destination.with_name(
        f".{destination.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        replace_with_retry(temporary, destination)
    finally:
        unlink_with_retry(temporary, suppress_errors=True)


def _with_token_context(
    usage: dict[str, Any],
    instance: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """A16：在 Token 用量摘要上附加上下文预算与最近裁剪时间（只读展示）。"""
    result = dict(usage) if isinstance(usage, dict) else {}
    result["context_budget"] = {}
    if instance and isinstance(instance.get("world_snapshot"), Mapping):
        rules = instance["world_snapshot"].get("rules") or {}
        if isinstance(rules, Mapping):
            result["context_budget"] = dict(rules.get("context_budget") or {})
    result["last_trim_at"] = ""
    return result


def _actor_projection_row(
    world: Mapping[str, Any],
    raw: Mapping[str, Any],
    *,
    profile: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach the ordinary display contract and remove raw profile payloads."""

    item = dict(raw)
    source = profile
    if source is None:
        candidate = item.get("card_profile") or item.get("draft_profile") or {}
        source = candidate if isinstance(candidate, Mapping) else {}
    view = project_actor_view(world, source, viewer_role="admin")
    # Participant columns are authoritative persisted display facts and are safe
    # fallbacks when an incomplete draft has not filled identity fields yet.
    if not str(view.get("title") or "").strip():
        fallback = str(item.get("character_name") or "").strip()
        if fallback:
            view["title"] = fallback
        else:
            view.setdefault("problems", []).append(
                {
                    "code": "projection.actor_title_missing",
                    "path": "actor.identity.name",
                    "message": "角色名称数据缺失",
                }
            )
    if not str(view.get("subtitle") or "").strip():
        fallback = str(item.get("character_code") or "").strip()
        if fallback:
            view["subtitle"] = fallback
    item.pop("card_profile", None)
    item.pop("draft_profile", None)
    item.pop("profile", None)
    item.pop("profile_json", None)
    item["actor_view"] = view
    return item


# ── D1（16_STAGED_CHARACTER_CREATION）：B/C 角色补充的 Web 安全投影 ────

_DELIVERY_CHANNEL_LABELS = {
    "webui_only": "仅面板展示",
    "pending": "私聊投递中",
    "leased": "私聊投递中",
    "partially_sent": "私聊投递中",
    "retry_wait": "等待重试投递",
    "delivered": "已送达私聊",
    "cancelled": "已取消",
    "permanently_failed": "投递失败（已达上限）",
}


def _delivery_channel_label(status: str) -> str:
    return _DELIVERY_CHANNEL_LABELS.get(
        str(status or "").strip(), "等待投递"
    )


def _project_supplement_offers(
    offers: list[dict[str, Any]],
    *,
    viewer_role: str,
    participant: Mapping[str, Any] | None = None,
    readonly: bool = False,
) -> list[dict[str, Any]]:
    """B/C 补充提议的语义 DTO。

    普通玩家视图绝不包含内部稳定 ID（offer_id / field_key / 候选 id /
    participant_id）；``ref`` 仅作为前端回传的不透明句柄，永不渲染。
    私密候选内容只出现在本人可见项与主持人视图中，且不进入群聊摘要。
    """
    privileged = str(viewer_role or "player") in {"dm", "admin"}
    result: list[dict[str, Any]] = []
    for raw in offers:
        if not isinstance(raw, Mapping):
            continue
        expired = bool(raw.get("expired"))
        candidates = [
            {
                "label": str(item.get("label") or ""),
                "description": str(item.get("description") or ""),
            }
            for item in (raw.get("candidates") or [])
            if isinstance(item, Mapping)
        ]
        bound = bool(
            str((participant or {}).get("private_origin") or "").strip()
        )
        item: dict[str, Any] = {
            "ref": str(raw.get("offer_id") or ""),
            "field_label": str(raw.get("field_label") or "角色资料"),
            "stage": str(raw.get("stage") or "B"),
            "stage_label": str(raw.get("stage_label") or "角色补充"),
            "state": str(raw.get("state") or "offered"),
            "expired": expired,
            "free_text": bool(raw.get("free_text")),
            "fallback": bool(raw.get("fallback")),
            "offer_round": int(raw.get("offer_round") or 0),
            "expires_after_rounds": int(
                raw.get("expires_after_rounds") or 0
            ),
            "candidates": candidates,
            "delivery_channel": (
                "仅面板展示"
                if not privileged and not bound
                else "私聊投递中"
                if not privileged
                else _delivery_channel_label(
                    str(raw.get("delivery_status") or "")
                )
            ),
            "can_confirm": False,
            "confirm_hint": "",
        }
        if privileged:
            item["character_name"] = str(
                raw.get("character_name") or "角色"
            )
            item["participant_id"] = str(raw.get("participant_id") or "")
            item["delivery_status"] = str(raw.get("delivery_status") or "")
            item["attempts"] = int(raw.get("attempts") or 0)
            item["last_error"] = str(raw.get("last_error") or "")
            item["trigger_source"] = str(raw.get("trigger_source") or "")
            item["offer_no"] = int(raw.get("offer_no") or 0)
            item["confirm_hint"] = (
                "只有角色本人可以确认；主持人与管理员只能查看状态。"
            )
        else:
            confirmable = bool(not expired and not readonly and bound)
            item["can_confirm"] = confirmable
            if readonly:
                item["confirm_hint"] = (
                    "副本已归档，角色补充只读；新的补充请在克隆副本中继续。"
                )
            elif not bound:
                item["confirm_hint"] = (
                    "该角色尚未通过私聊绑定，无法在面板确认。"
                    "请本人私聊 BOT 发送：/团 当前"
                )
        result.append(item)
    return result


def _supplement_action_message(
    action: str,
    result: Mapping[str, Any],
) -> str:
    label = str(result.get("field_label") or "角色资料")
    if action == "confirm":
        return (
            f"「{label}」已写入角色卡。系统已更新角色卡阶段，"
            "并仅向群聊发送安全公开摘要。\n"
            "下一步：刷新面板查看是否还有待确认项目。"
        )
    if action == "postpone":
        return (
            f"已暂缓「{label}」。系统没有修改角色卡；"
            "该项目会在后续剧情窗口重新出现。"
        )
    if action == "cancel":
        return (
            f"已取消「{label}」。系统没有修改角色卡；"
            "第一幕保底检查仍可能重新提出缺失项目。"
        )
    if action == "reject":
        return (
            f"已更换「{label}」候选。系统已记录你拒绝的候选，"
            "后续不会立即原样重复；请查看新的候选。"
        )
    return "角色补充已处理。"


def _revision_projection_rows(
    world: Mapping[str, Any],
    rows: list[Mapping[str, Any]],
    roster: list[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    roster_views = {
        str(item.get("id") or ""): _actor_projection_row(world, item)["actor_view"]
        for item in roster or []
        if isinstance(item, Mapping) and str(item.get("id") or "")
    }
    result: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        profile = raw.get("profile")
        profile = profile if isinstance(profile, Mapping) else {}
        item = dict(raw)
        item.pop("profile", None)
        item.pop("profile_json", None)
        item.pop("card_profile", None)
        item.pop("draft_profile", None)
        candidate = _actor_projection_row(world, item, profile=profile)["actor_view"]
        item["actor_view"] = candidate
        item["candidate_actor_view"] = candidate
        item["base_actor_view"] = roster_views.get(
            str(item.get("participant_id") or ""),
            {"schema": "tavern-actor-view/1.0.0-rc10", "title": "", "subtitle": "", "sections": [], "problems": [{"code": "projection.base_actor_missing", "path": "card_revision", "message": "当前角色卡投影不可用"}]},
        )
        result.append(item)
    return result


def _safe(fn: Any, default: Any) -> Any:
    """A23: 模块级逐字段容错助手（此前误定义在类内且缺 self，导致
    extensions/hook_events/meta_capabilities 抛 NameError）。"""
    try:
        return fn()
    except Exception:
        return default


def _extension_catalog(registry: Any) -> tuple[dict[str, list[str]], list[str]]:
    """Return a JSON-safe extension catalog while isolating malformed items."""
    if registry is None:
        return {}, []
    errors: list[str] = []
    try:
        raw_catalog = registry.list()
    except Exception as exc:
        return {}, [f"扩展注册表读取失败：{type(exc).__name__}: {str(exc)[:160]}"]
    if not isinstance(raw_catalog, Mapping):
        return {}, ["扩展注册表返回了无效的数据结构"]

    catalog: dict[str, list[str]] = {}
    for raw_kind, raw_names in raw_catalog.items():
        try:
            kind = str(raw_kind).strip()
            if not kind:
                raise ValueError("扩展类型为空")
            if isinstance(raw_names, str):
                candidates = (raw_names,)
            elif isinstance(raw_names, Mapping):
                candidates = raw_names.keys()
            else:
                candidates = raw_names or ()
            names: list[str] = []
            iterator = iter(candidates)
            while True:
                try:
                    candidate = next(iterator)
                except StopIteration:
                    break
                except Exception as exc:
                    errors.append(
                        f"{kind} 扩展列表读取中断：{type(exc).__name__}: {str(exc)[:120]}"
                    )
                    break
                try:
                    name = str(candidate).strip()
                    if name:
                        names.append(name)
                except Exception as exc:
                    errors.append(
                        f"{kind} 中有一个扩展项无法序列化：{type(exc).__name__}"
                    )
            catalog[kind] = sorted(set(names))
        except Exception as exc:
            errors.append(
                f"一个扩展类型无法读取：{type(exc).__name__}: {str(exc)[:120]}"
            )
    return dict(sorted(catalog.items())), errors


def _hook_catalog(hooks: Any) -> tuple[dict[str, int], list[str]]:
    """Return JSON-safe subscription counts without trusting extension data."""
    if hooks is None:
        return {}, []
    try:
        raw_catalog = hooks.list_subscriptions()
    except Exception as exc:
        return {}, [f"事件订阅表读取失败：{type(exc).__name__}: {str(exc)[:160]}"]
    if not isinstance(raw_catalog, Mapping):
        return {}, ["事件订阅表返回了无效的数据结构"]

    catalog: dict[str, int] = {}
    errors: list[str] = []
    for raw_event, raw_count in raw_catalog.items():
        try:
            event = str(raw_event).strip()
            count = max(0, int(raw_count))
            if not event:
                raise ValueError("事件名为空")
            catalog[event] = count
        except Exception as exc:
            errors.append(
                f"一个事件订阅项无法读取：{type(exc).__name__}: {str(exc)[:120]}"
            )
    return dict(sorted(catalog.items())), errors

__all__ = [name for name in globals() if not name.startswith('__')]
