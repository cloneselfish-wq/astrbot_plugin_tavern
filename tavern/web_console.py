from __future__ import annotations

import asyncio
import json
import hashlib
import os
import re
import shutil
import sqlite3
import stat
import uuid
import zipfile
from collections import OrderedDict
from contextlib import closing
from pathlib import Path, PurePosixPath
from typing import Any

from astrbot.api.web import (
    PluginUploadFile,
    error_response,
    file_response,
    json_response,
    request,
    stream_response,
)

from .config import TavernConfig
from .constants import (
    PLUGIN_NAME,
    PLUGIN_VERSION,
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
from .operations import recovery_summary
from .world_migration import compare_world_contracts
from .world_preflight import inspect_world_package
from .world_import import world_edit_payload, world_import_payload
from .rule_runtime import RuleRuntime
from .entity_registry import EntityRegistry
from .storage import (
    file_sha256,
    next_timestamped_path,
    replace_with_retry,
    unlink_with_retry,
)


_BACKUP_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MAX_BACKUP_UNCOMPRESSED = 4 * 1024 * 1024 * 1024
# 世界包导入冲突的前缀标记：前端据此弹出「覆盖为新修订 / 另存副本」决策弹窗。
_WORLD_CONFLICT_PREFIX = "导入冲突"
_WORLD_SIMULATE_MAX_BYTES = 1_000_000
_WORLD_SIMULATE_MAX_DEPTH = 40


def _json_size(obj: Any) -> int:
    try:
        return len(json.dumps(obj, ensure_ascii=False))
    except (TypeError, ValueError):
        return 0


def _json_depth(obj: Any, limit: int = _WORLD_SIMULATE_MAX_DEPTH) -> bool:
    """粗略判断 JSON 嵌套深度是否超过 limit（防计算型 DoS）。"""

    def walk(value: Any, depth: int) -> bool:
        if depth > limit:
            return True
        if isinstance(value, dict):
            return any(walk(item, depth + 1) for item in value.values())
        if isinstance(value, (list, tuple)):
            return any(walk(item, depth + 1) for item in value)
        return False

    return walk(obj, 0)


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


def _stage_group_files(
    archive: zipfile.ZipFile,
    stage_dir: Path,
) -> list[tuple[Path, PurePosixPath]]:
    staged: list[tuple[Path, PurePosixPath]] = []
    for info in archive.infolist():
        if info.is_dir():
            continue
        relative = _safe_backup_member(info.filename)
        if not relative.parts or relative.parts[0] != "groups":
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


class TavernWebConsole:
    def __init__(
        self,
        *,
        context: Any,
        plugin_config: Any,
        database: TavernDatabase,
        broker: EventBroker,
        data_dir: Path,
        logger: Any,
        allow_group: Any,
        config_lock: Any,
    ) -> None:
        self.context = context
        self.plugin_config = plugin_config
        self.database = database
        self.broker = broker
        self.data_dir = Path(data_dir)
        self.logger = logger
        self.allow_group = allow_group
        self.config_lock = config_lock
        # 0.11.1：按 (slug, revision) 缓存 RuleRuntime，避免每次
        # world_simulate 全量重建 EntityRegistry/CapabilityService/EventPipeline。
        # RuleRuntime 在 __init__ 后只读（resolve 不修改内部状态），可安全复用。
        self._rule_runtime_cache: "OrderedDict[tuple[str, int], RuleRuntime]" = (
            OrderedDict()
        )
        self._register_routes()

    def _cached_rule_runtime(self, world: Mapping[str, Any]) -> RuleRuntime:
        slug = str(world.get("slug") or "").strip()
        revision = int(world.get("revision") or 0)
        if not slug:
            return RuleRuntime(world)
        key = (slug, revision)
        cached = self._rule_runtime_cache.get(key)
        if cached is None:
            cached = RuleRuntime(world)
            self._rule_runtime_cache[key] = cached
            if len(self._rule_runtime_cache) > 8:
                self._rule_runtime_cache.popitem(last=False)
        return cached

    def _register(self, path: str, handler: Any, methods: list[str], desc: str) -> None:
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/{path}",
            handler,
            methods,
            desc,
        )

    def _register_routes(self) -> None:
        routes = [
            ("overview", self.overview, ["GET"], "Tavern overview"),
            ("worlds", self.worlds, ["GET"], "List worlds"),
            ("worlds/preflight", self.world_preflight, ["POST"], "Inspect world package"),
            ("worlds/migration", self.world_migration, ["POST"], "Compare frozen world contract"),
            ("worlds/save", self.world_save, ["POST"], "Save world"),
            ("worlds/order", self.world_order, ["POST"], "Reorder world"),
            ("worlds/simulate", self.world_simulate, ["POST"], "Dry-run v5 rules"),
            ("worlds/archive", self.world_archive, ["POST"], "Archive world"),
            ("worlds/restore", self.world_restore, ["POST"], "Restore world"),
            ("characters", self.characters, ["GET"], "List characters"),
            (
                "characters/save",
                self.character_save,
                ["POST"],
                "Save character",
            ),
            (
                "characters/delete",
                self.character_delete,
                ["POST"],
                "Delete character",
            ),
            (
                "worlds/import",
                self.world_import,
                ["POST"],
                "Import world package JSON",
            ),
            (
                "characters/import",
                self.character_import,
                ["POST"],
                "Import resident NPCs/characters JSON",
            ),
            ("sessions", self.sessions, ["GET"], "List sessions"),
            (
                "sessions/detail",
                self.session_detail,
                ["GET"],
                "Session detail",
            ),
            ("sessions/recovery", self.session_recovery, ["GET"], "Inspect workflow recovery state"),
            ("sessions/diagnostics", self.session_diagnostics, ["GET"], "Export redacted diagnostics"),
            ("sessions/rescue", self.session_rescue, ["POST"], "Run precise workflow recovery"),
            ("sessions/card-revisions", self.session_card_revisions, ["GET", "POST"], "Manage character card revisions"),
            (
                "sessions/action",
                self.session_action,
                ["POST"],
                "Session action",
            ),
            (
                "sessions/state",
                self.session_state,
                ["POST"],
                "Edit session state",
            ),
            (
                "sessions/turn-order",
                self.session_turn_order,
                ["POST"],
                "Edit multiplayer turn order",
            ),
            (
                "sessions/time-rules",
                self.session_time_rules,
                ["POST"],
                "Edit instance timing rules",
            ),
            (
                "sessions/rules",
                self.session_rules,
                ["POST"],
                "Edit session rules and progress",
            ),
            (
                "sessions/npc",
                self.session_npc,
                ["POST"],
                "Create or edit a session NPC",
            ),
            (
                "sessions/timer",
                self.session_timer,
                ["POST"],
                "Control persistent timer",
            ),
            (
                "sessions/timer-policy",
                self.session_timer_policy,
                ["POST"],
                "Control countdown categories",
            ),
            (
                "sessions/token-quota",
                self.session_token_quota,
                ["POST"],
                "Control token quotas",
            ),
            (
                "sessions/card-review",
                self.session_card_review,
                ["POST"],
                "Review a character card",
            ),
            (
                "sessions/permission",
                self.session_permission,
                ["POST"],
                "Grant instance role",
            ),
            (
                "sessions/participant",
                self.session_participant,
                ["POST"],
                "Manage participant status",
            ),
            (
                "groups/remark",
                self.group_remark,
                ["POST"],
                "Save a group remark",
            ),
            (
                "groups/token-usage",
                self.group_token_usage,
                ["GET"],
                "Read group token usage and quota",
            ),
            (
                "groups/token-quota",
                self.group_token_quota,
                ["POST"],
                "Control a group token quota",
            ),
            ("players", self.players, ["GET"], "List players"),
            ("players/save", self.player_save, ["POST"], "Save player"),
            (
                "players/delete",
                self.player_delete,
                ["POST"],
                "Delete player",
            ),
            ("memories", self.memories, ["GET"], "List memories"),
            ("memories/save", self.memory_save, ["POST"], "Save memory"),
            (
                "memories/delete",
                self.memory_delete,
                ["POST"],
                "Delete memory",
            ),
            ("snapshots", self.snapshots, ["GET"], "List snapshots"),
            (
                "snapshots/create",
                self.snapshot_create,
                ["POST"],
                "Create snapshot",
            ),
            (
                "snapshots/restore",
                self.snapshot_restore,
                ["POST"],
                "Restore snapshot",
            ),
            (
                "snapshots/delete",
                self.snapshot_delete,
                ["POST"],
                "Delete snapshot",
            ),
            (
                "archives/delete",
                self.archive_delete,
                ["POST"],
                "Delete independent save archive",
            ),
            ("audit", self.audit, ["GET"], "Audit log"),
            ("providers", self.providers, ["GET"], "List chat providers"),
            ("settings", self.settings, ["GET"], "Tavern settings"),
            (
                "settings/save",
                self.settings_save,
                ["POST"],
                "Save Tavern settings",
            ),
            (
                "backup/export",
                self.backup_export,
                ["GET"],
                "Export Tavern backup",
            ),
            (
                "backup/import/<mode>",
                self.backup_import,
                ["POST"],
                "Import Tavern backup",
            ),
            ("events", self.events, ["GET"], "Tavern activity stream"),
        ]
        for route in routes:
            self._register(*route)

    @staticmethod
    def _username() -> str:
        username = str(request.username or "").strip()
        if not username:
            raise PermissionError("需要登录 AstrBot 管理后台")
        return username

    @classmethod
    def _actor(cls) -> str:
        return f"web:{cls._username()}"

    def _handle_error(self, exc: Exception):
        if isinstance(exc, PermissionError):
            return error_response(str(exc), status_code=401)
        if isinstance(exc, DatabaseNotFoundError):
            return error_response(str(exc), status_code=404)
        if isinstance(exc, DatabaseConflictError):
            return error_response(str(exc), status_code=409)
        if isinstance(exc, (InvalidTransitionError, ValueError, TypeError)):
            return error_response(str(exc), status_code=400)
        if isinstance(exc, sqlite3.IntegrityError):
            return error_response(
                "数据冲突：标识、群会话或名称可能已存在",
                status_code=409,
            )
        self.logger.exception("AI 酒馆 WebUI 请求失败")
        return error_response("服务器内部错误", status_code=500)

    async def _payload(self) -> dict[str, Any]:
        value = await request.json(default={})
        if not isinstance(value, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return value

    async def overview(self):
        try:
            self._username()
            result = await self.database.overview()
            result["plugin_version"] = PLUGIN_VERSION
            result["sessions"] = (await self.database.list_sessions())[:8]
            config = TavernConfig.from_mapping(self.plugin_config)
            result["security"] = {
                "admin_count": len(config.admin_ids),
                "allowed_group_count": len(config.allowed_group_ids),
                "whitelist_required": config.require_group_whitelist,
                "ready": bool(config.admin_ids),
            }
            return json_response(result)
        except Exception as exc:
            return self._handle_error(exc)

    async def worlds(self):
        try:
            self._username()
            archived_value = request.query.get("include_archived", "")
            include_archived = str(archived_value).strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
            return json_response(
                {"items": await self.database.list_worlds(include_archived)}
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def world_save(self):
        try:
            submitted = await self._payload()
            current = None
            world_id = str(submitted.get("id") or "").strip()
            if world_id:
                current = await self.database.get_world(world_id)
            payload = world_edit_payload(submitted, current)
            report = inspect_world_package(payload)
            if not report["compatible"]:
                messages = [
                    item["message"]
                    for item in report["issues"]
                    if item["level"] == "error"
                ]
                raise ValueError("世界包体检未通过：" + "；".join(messages[:5]))
            item = await self.database.save_world(payload, self._actor())
            await self.broker.publish({"type": "world", "action": "save"})
            return json_response({"item": item, "preflight": report})
        except Exception as exc:
            return self._handle_error(exc)

    async def world_preflight(self):
        try:
            self._username()
            payload = await self._payload()
            world_ref = str(payload.get("world_ref") or "")
            world = (
                await self.database.get_world(world_ref)
                if world_ref
                else payload.get("world", payload)
            )
            if not isinstance(world, dict):
                raise ValueError("world 必须是 JSON 对象")
            return json_response({"report": inspect_world_package(world)})
        except Exception as exc:
            return self._handle_error(exc)

    async def world_order(self):
        try:
            payload = await self._payload()
            item = await self.database.set_world_sort_order(
                str(payload.get("id") or ""),
                int(payload.get("sort_order") or 1),
                self._actor(),
            )
            await self.broker.publish({"type": "world", "action": "reorder"})
            return json_response({"item": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def world_simulate(self):
        try:
            # 0.11.1：补齐鉴权（此前是唯一漏掉 _username() 的路由），
            # 并对可投递的 world/intent/context 做体积与深度上限，防计算型 DoS。
            self._username()
            payload = await self._payload()
            world_ref = str(payload.get("world_ref") or "")
            world = (
                await self.database.get_world(world_ref)
                if world_ref
                else payload.get("world")
            )
            if not isinstance(world, dict):
                raise ValueError("请提供 world_ref 或 world")
            if _json_size(world) > _WORLD_SIMULATE_MAX_BYTES:
                raise ValueError("世界包过大，拒绝执行规则模拟")
            if _json_depth(world):
                raise ValueError("世界包嵌套过深，拒绝执行规则模拟")
            report = inspect_world_package(world)
            if not report["compatible"]:
                raise ValueError("世界包体检未通过，不能执行规则模拟")
            intent = payload.get("intent")
            context = payload.get("context", {})
            if not isinstance(intent, dict) or not isinstance(context, dict):
                raise ValueError("intent 与 context 必须是 JSON 对象")
            if _json_depth({"intent": intent, "context": context}):
                raise ValueError("intent 或 context 嵌套过深")
            result = self._cached_rule_runtime(world).resolve_action_intent(
                intent, context, dry_run=True,
                world_snapshot_id=f"preview:{world.get('slug', '')}",
            )
            return json_response({"result": result, "preflight": report})
        except Exception as exc:
            return self._handle_error(exc)

    async def world_migration(self):
        try:
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            instance = await self.database.get_instance_config(session_id)
            candidate_ref = str(payload.get("candidate_world_ref") or "")
            candidate = (
                await self.database.get_world(candidate_ref)
                if candidate_ref
                else payload.get("candidate_world")
            )
            if not isinstance(candidate, dict):
                raise ValueError("请提供候选世界包或世界标识")
            return json_response(
                {
                    "report": compare_world_contracts(
                        instance.get("world_snapshot") or {}, candidate
                    )
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def world_archive(self):
        try:
            payload = await self._payload()
            world_id = str(payload.get("id", ""))
            world = await self.database.get_world(world_id)
            config = TavernConfig.from_mapping(self.plugin_config)
            if world["slug"] == config.default_world_slug:
                raise ValueError(
                    "该世界是当前默认世界，请先在设置中更换默认世界"
                )
            item = await self.database.archive_world(
                world_id,
                self._actor(),
            )
            await self.broker.publish({"type": "world", "action": "archive"})
            return json_response({"item": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def world_restore(self):
        try:
            payload = await self._payload()
            item = await self.database.restore_world(
                str(payload.get("id", "")),
                self._actor(),
            )
            await self.broker.publish({"type": "world", "action": "restore"})
            return json_response({"item": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def characters(self):
        try:
            self._username()
            world_id = str(request.query.get("world_id", "") or "")
            return json_response(
                {"items": await self.database.list_characters(world_id)}
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def character_save(self):
        try:
            item = await self.database.save_character(
                await self._payload(),
                self._actor(),
            )
            return json_response({"item": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def character_delete(self):
        try:
            payload = await self._payload()
            await self.database.delete_character(
                str(payload.get("id", "")),
                self._actor(),
            )
            return json_response({"deleted": True})
        except Exception as exc:
            return self._handle_error(exc)

    async def world_import(self):
        """导入世界包 JSON：校验结构后按 slug 新建或更新世界。"""
        try:
            self._username()
            request_payload = await self._payload()
            import_mode = str(request_payload.get("import_mode") or "auto")
            payload = request_payload.get("world", request_payload)
            if not isinstance(payload, dict):
                raise ValueError("世界包 JSON 格式无效：顶层必须是一个对象")
            if not payload.get("slug"):
                raise ValueError("世界包缺少必填字段 slug")
            if not payload.get("name"):
                raise ValueError("世界包缺少必填字段 name（世界名称）")
            if not payload.get("system_prompt"):
                raise ValueError("世界包缺少必填字段 system_prompt（世界设定）")
            rules = payload.get("rules")
            if rules is not None and not isinstance(rules, dict):
                raise ValueError("rules 必须是 JSON 对象")
            initial_state = payload.get("initial_state")
            if initial_state is not None and not isinstance(initial_state, dict):
                raise ValueError("initial_state 必须是 JSON 对象")
            report = inspect_world_package(payload)
            if not report["compatible"]:
                messages = [
                    item["message"]
                    for item in report["issues"]
                    if item["level"] == "error"
                ]
                raise ValueError("世界包体检未通过：" + "；".join(messages[:5]))
            import_payload = world_import_payload(payload)
            mode = "created"
            try:
                existing = await self.database.get_world(str(payload["slug"]))
                existing_package = world_import_payload(existing)
                incoming_hash = hashlib.sha256(
                    json.dumps(import_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest()
                existing_hash = hashlib.sha256(
                    json.dumps(existing_package, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest()
                if incoming_hash == existing_hash:
                    return json_response({"item": existing, "mode": "identical", "preflight": report})
                if import_mode == "copy":
                    copy_slug = str(request_payload.get("copy_slug") or f"{payload['slug']}-copy")
                    suffix = 2
                    while True:
                        try:
                            await self.database.get_world(copy_slug)
                            copy_slug = f"{payload['slug']}-copy-{suffix}"
                            suffix += 1
                        except DatabaseNotFoundError:
                            break
                    import_payload["slug"] = copy_slug
                    mode = "copied"
                else:
                    old_version = str(existing.get("world_content_version") or existing.get("version") or "")
                    new_version = str(payload.get("world_content_version") or payload.get("version") or "")
                    if old_version and new_version and old_version == new_version and import_mode != "force_revision":
                        raise DatabaseConflictError(
                            f"{_WORLD_CONFLICT_PREFIX}：同 slug、同内容版本但内容不同；"
                            "请选择“覆盖为新修订”或“另存副本”"
                        )
                    old_key = _content_version_key(old_version)
                    new_key = _content_version_key(new_version)
                    if (
                        old_key is not None
                        and new_key is not None
                        and new_key < old_key
                        and import_mode != "force_revision"
                    ):
                        raise DatabaseConflictError(
                            f"{_WORLD_CONFLICT_PREFIX}：导入包的内容版本低于现有世界；"
                            "如确需回退，请明确选择“覆盖为新修订”"
                        )
                    import_payload["id"] = existing["id"]
                    import_payload["revision"] = existing["revision"]
                    mode = "updated"
            except DatabaseNotFoundError:
                pass
            item = await self.database.save_world(import_payload, self._actor())
            await self.broker.publish({"type": "world", "action": "save"})
            return json_response({"item": item, "mode": mode, "preflight": report})
        except Exception as exc:
            return self._handle_error(exc)

    async def character_import(self):
        """导入常驻角色/NPC JSON：校验后批量写入指定世界。"""
        try:
            self._username()
            payload = await self._payload()
            world_ref = (
                payload.get("world_id")
                or payload.get("world_slug")
                or payload.get("worldRef")
                or ""
            )
            if not str(world_ref).strip():
                raise ValueError(
                    "请指定目标世界：world_id 或 world_slug 不能为空"
                )
            world = await self.database.get_world(str(world_ref).strip())
            template_version = int(payload.get("template_version", 1) or 1)
            if template_version not in {1, 2}:
                raise ValueError("只接受 NPC 导入模板 v1 或 v2")
            items = (
                payload.get("items")
                or payload.get("npcs")
                or payload.get("characters")
            )
            if not isinstance(items, list) or not items:
                raise ValueError(
                    "常驻角色数据必须是非空数组（字段 items / npcs）"
                )
            existing_items = await self.database.list_characters(world["id"])
            existing_by_slug = {
                str(item.get("slug")): item for item in existing_items
                if item.get("slug")
            }
            existing_by_name = {
                str(item.get("name")): item for item in existing_items
                if item.get("name")
            }
            registry = EntityRegistry(world) if int(world.get("world_schema_version", 0) or 0) >= 5 else None
            input_slugs: set[str] = set()
            created = []
            for index, raw in enumerate(items):
                if not isinstance(raw, dict):
                    raise ValueError(f"第 {index + 1} 个角色项必须是对象")
                name = str(raw.get("name") or "").strip()
                if not name:
                    raise ValueError(f"第 {index + 1} 个角色缺少 name（名称）")
                role = str(raw.get("role") or "npc").strip() or "npc"
                npc_profile = raw.get("profile")
                if npc_profile is not None and not isinstance(npc_profile, dict):
                    raise ValueError(
                        f"角色「{name}」的 profile 必须是 JSON 对象"
                    )
                profile = dict(npc_profile) if isinstance(npc_profile, dict) else {}
                raw_slug = str(raw.get("slug") or "").strip()
                if raw_slug:
                    if raw_slug in input_slugs:
                        raise ValueError(f"NPC 导入包含重复 slug：{raw_slug}")
                    input_slugs.add(raw_slug)
                    validate_slug(raw_slug)
                if registry is not None:
                    ref_fields = {
                        "capability_refs": "capability",
                        "resource_refs": "resource",
                        "runtime_effect_refs": "runtime_effect",
                        "object_refs": "object",
                    }
                    for field, expected_type in ref_fields.items():
                        values = profile.get(field, [])
                        if not isinstance(values, list):
                            raise ValueError(f"角色「{name}」的 {field} 必须是数组")
                        for ref in values:
                            registry.resolve(ref, expected_type)
                    resources = profile.get("resources", {})
                    if resources and not isinstance(resources, dict):
                        raise ValueError(f"角色「{name}」的 resources 必须是对象")
                    if isinstance(resources, dict):
                        for ref in resources:
                            registry.resolve(ref, "resource")
                private = raw.get("private_direction") or raw.get("prompt") or ""
                if private:
                    profile.setdefault("private_direction", private)
                character_payload = {
                    "world_id": world["id"],
                    "name": name,
                    "role": role,
                    "profile": profile,
                    "prompt": str(private),
                    "enabled": 1,
                }
                prior = existing_by_slug.get(raw_slug) if raw_slug else None
                if prior is None:
                    # v1 did not require stable slugs, so name fallback remains
                    # solely for legacy imports.
                    prior = existing_by_name.get(name)
                if prior:
                    character_payload["id"] = prior["id"]
                    character_payload["revision"] = prior["revision"]
                    character_payload["slug"] = prior["slug"]
                    character_payload["sort_order"] = prior.get("sort_order", 0)
                    character_payload["enabled"] = prior.get("enabled", 1)
                else:
                    character_payload["slug"] = raw_slug or f"npc_{uuid.uuid4().hex[:12]}"
                if "sort_order" in raw:
                    character_payload["sort_order"] = int(raw["sort_order"])
                created.append(
                    await self.database.save_character(
                        character_payload, self._actor()
                    )
                )
            await self.broker.publish({"type": "world", "action": "save"})
            return json_response(
                {
                    "created": len(created),
                    "world_id": world["id"],
                    "world_name": world.get("name"),
                    "items": created,
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def sessions(self):
        try:
            self._username()
            result = await self.database.search_sessions(
                str(request.query.get("q", "") or ""),
                str(request.query.get("scope", "all") or "all"),
                request.query.get("page", 1, type=int),
                request.query.get("page_size", 20, type=int),
            )
            result["options"] = await self.database.list_session_options()
            return json_response(result)
        except Exception as exc:
            return self._handle_error(exc)

    async def session_detail(self):
        try:
            self._username()
            session_id = str(request.query.get("id", "") or "")
            session = await self.database.get_session(session_id)
            return json_response(
                {
                    "session": session,
                    "players": await self.database.list_players(session_id),
                    "roster": await self.database.list_roster(session_id),
                    "turn": await self.database.get_turn_status(session_id),
                    "control": await self.database.get_control_state(session_id),
                    "events": await self.database.recent_events(session_id, 80),
                    "snapshots": await self.database.list_snapshots(session_id),
                    "instance_config": (
                        await self.database.get_instance_config(session_id)
                    ),
                    "timers": await self.database.list_timers(session_id),
                    "timer_policy": (
                        await self.database.get_timer_policy(session_id)
                    ),
                    "token_usage": (
                        await self.database.token_usage_summary(session_id)
                    ),
                    "choice": await self.database.active_choice_set(session_id),
                    "vote": await self.database.active_vote(session_id),
                    "bans": await self.database.list_bans(session_id),
                    "permissions": (
                        await self.database.list_permission_grants(session_id)
                    ),
                    "return_requests": (
                        await self.database.list_return_requests(session_id)
                    ),
                    "preflight": (
                        await self.database.opening_preflight(session_id)
                    ),
                    "rule_state": (
                        await self.database.get_session_rule_state(session_id)
                    ),
                    "session_characters": (
                        await self.database.list_session_characters(session_id)
                    ),
                    "story_ledger": (
                        await self.database.list_story_ledger(session_id)
                    ),
                    "scene_clocks": (
                        await self.database.list_scene_clocks(session_id)
                    ),
                    "memories": await self.database.list_memories(
                        session_id,
                        "",
                        500,
                        include_invalidated=True,
                    ),
                    "archive": (
                        await self.database.get_session_archive(session_id)
                    ),
                    "storage": (
                        await self.database.get_storage_info(session_id)
                    ),
                    "operations": (
                        await self.database.list_session_operations(session_id, 50)
                    ),
                    "card_revisions": (
                        await self.database.list_card_revisions(session_id)
                    ),
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def session_recovery(self):
        try:
            self._username()
            session_id = str(request.query.get("id", "") or "")
            session = await self.database.get_session(session_id)
            operations = await self.database.list_session_operations(session_id, 100)
            choice = await self.database.active_choice_set(session_id)
            vote = await self.database.active_vote(session_id)
            return json_response(
                {
                    "recovery": recovery_summary(
                        operations,
                        session_state=str(session.get("state") or ""),
                        has_active_choices=bool(choice),
                        has_active_vote=bool(vote),
                    )
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def session_diagnostics(self):
        try:
            self._username()
            session_id = str(request.query.get("id", "") or "")
            report = await build_diagnostic_report(self.database, session_id)
            export_dir = self.data_dir / "exports"
            export_dir.mkdir(parents=True, exist_ok=True)
            path = next_timestamped_path(export_dir, "tavern_diagnostic", ".zip")
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            payload = (
                json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n"
            ).encode("utf-8")
            try:
                with zipfile.ZipFile(
                    temporary,
                    "w",
                    compression=zipfile.ZIP_DEFLATED,
                    compresslevel=6,
                ) as archive:
                    archive.writestr("diagnostic.json", payload)
                    archive.writestr(
                        "README.txt",
                        "AI 酒馆脱敏故障报告。用户 ID 已哈希，密钥、私聊来源、私人字段与完整系统提示词未导出。\n",
                    )
                replace_with_retry(temporary, path)
            finally:
                unlink_with_retry(temporary, suppress_errors=True)
            return file_response(path, filename=path.name, content_type="application/zip")
        except Exception as exc:
            return self._handle_error(exc)

    async def session_rescue(self):
        try:
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            service = EmergencyService(self.database)
            result = await service.execute(
                session_id,
                str(payload.get("action") or ""),
                payload,
                self._actor(),
            )
            await self.broker.publish(
                {"type": "session", "action": "rescue", "session_id": session_id}
            )
            return json_response({"result": result})
        except Exception as exc:
            return self._handle_error(exc)

    async def session_card_revisions(self):
        try:
            if request.method == "GET":
                self._username()
                session_id = str(request.query.get("id", "") or "")
                return json_response(
                    {"items": await self.database.list_card_revisions(session_id)}
                )
            payload = await self._payload()
            action = str(payload.get("action") or "request").lower()
            if action == "request":
                item = await self.database.request_card_revision(
                    str(payload.get("session_id") or ""),
                    str(payload.get("participant_ref") or ""),
                    payload.get("profile_patch") or {},
                    payload.get("stats_patch") or {},
                    self._actor(),
                    str(payload.get("note") or ""),
                )
            elif action in {"approve", "reject"}:
                item = await self.database.review_card_revision(
                    str(payload.get("request_id") or ""),
                    action == "approve",
                    self._actor(),
                    str(payload.get("note") or ""),
                )
            else:
                raise ValueError("不支持的角色卡修订操作")
            return json_response({"item": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def group_remark(self):
        try:
            payload = await self._payload()
            item = await self.database.save_group_remark(
                str(payload.get("platform_id") or ""),
                str(payload.get("group_id") or ""),
                str(payload.get("remark") or ""),
                self._actor(),
                int(payload.get("revision") or 0),
            )
            await self.broker.publish(
                {
                    "type": "group",
                    "action": "remark",
                    "platform_id": item["platform_id"],
                    "group_id": item["group_id"],
                }
            )
            return json_response({"item": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def group_token_usage(self):
        try:
            self._username()
            item = await self.database.group_token_usage_summary(
                str(request.query.get("platform_id", "") or ""),
                str(request.query.get("group_id", "") or ""),
            )
            return json_response({"usage": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def group_token_quota(self):
        try:
            payload = await self._payload()
            item = await self.database.set_group_token_quota(
                str(payload.get("platform_id") or ""),
                str(payload.get("group_id") or ""),
                window_seconds=int(
                    payload.get("window_seconds") or 86_400
                ),
                token_limit=int(payload.get("token_limit") or 500_000),
                enabled=bool(payload.get("enabled", True)),
                actor_id=self._actor(),
            )
            await self.broker.publish(
                {
                    "type": "token",
                    "action": "group_quota",
                    "platform_id": item["platform_id"],
                    "group_id": item["group_id"],
                }
            )
            return json_response({"usage": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def session_action(self):
        try:
            payload = await self._payload()
            actor = self._actor()
            action = str(payload.get("action", "")).strip().lower()
            if action == "create":
                platform_id = str(payload.get("platform_id") or "")
                group_id = str(payload.get("group_id") or "")
                session = await self.database.ensure_session(
                    platform_id,
                    group_id,
                    str(payload.get("unified_origin") or ""),
                    str(payload.get("world_ref") or ""),
                    actor,
                    str(payload.get("instance_slug") or ""),
                    str(payload.get("instance_name") or ""),
                )
                config = TavernConfig.from_mapping(self.plugin_config)
                world = await self.database.get_world(session["world_id"])
                world_rules = world.get("rules") or {}
                world_time = (
                    world_rules.get("time_rules")
                    if isinstance(world_rules, dict)
                    else {}
                )
                await self.database.save_instance_time_rules(
                    session["id"],
                    normalize_time_rules(
                        {
                            **dict(config.time_rules),
                            **(
                                dict(world_time)
                                if isinstance(world_time, dict)
                                else {}
                            ),
                        }
                    ),
                    actor,
                )
                await self.database.grant_permission(
                    session["id"],
                    actor,
                    "host",
                    actor,
                )
                await self.allow_group(
                    group_id=group_id,
                    platform_id=platform_id,
                    actor_id=actor,
                    source="web_session_create",
                )
            elif action == "clone":
                session = await self.database.clone_session(
                    str(payload.get("session_id") or ""),
                    actor,
                    instance_slug=str(
                        payload.get("instance_slug") or ""
                    ),
                    instance_name=str(
                        payload.get("instance_name") or ""
                    ),
                    snapshot_ref=str(
                        payload.get("snapshot_ref") or ""
                    ),
                    candidate_world_ref=str(
                        payload.get("candidate_world_ref") or ""
                    ),
                )
            else:
                session_id = str(payload.get("session_id") or "")
                if action == "force_ready":
                    result = await self.database.force_all_ready(
                        session_id,
                        actor,
                    )
                    await self.broker.publish(
                        {
                            "type": "session",
                            "action": action,
                            "session_id": session_id,
                        }
                    )
                    return json_response({"result": result})
                if action == "dm_enable":
                    result = await self.database.enable_dm_mode(
                        session_id,
                        str(payload.get("dm_user_id") or actor),
                        actor,
                    )
                    return json_response({"control": result})
                if action == "dm_directive":
                    result = await self.database.set_dm_directive(
                        session_id,
                        str(payload.get("directive") or ""),
                        str(payload.get("dm_user_id") or actor),
                    )
                    return json_response({"control": result})
                if action == "dm_direct":
                    session = await self.database.get_session(session_id)
                    dm_user_id = str(payload.get("dm_user_id") or "")
                    result = await self.database.commit_dm_beat(
                        session_id=session_id,
                        expected_revision=int(session["revision"]),
                        dm_user_id=dm_user_id,
                        instruction=str(payload.get("narrative") or ""),
                        narrative=str(payload.get("narrative") or ""),
                        world_state=session["world_state"],
                        direct=True,
                    )
                    return json_response({"result": result})
                if action == "dm_disable":
                    result = await self.database.disable_dm_mode(session_id, actor)
                    return json_response({"control": result})
                if action == "delete":
                    result = await self.database.delete_session(
                        session_id,
                        actor,
                        str(payload.get("confirm_name") or ""),
                    )
                    await self.broker.publish(
                        {
                            "type": "session",
                            "action": action,
                            "session_id": session_id,
                        }
                    )
                    return json_response({"result": result})
                if action == "perform":
                    result = await self.database.activate_story(
                        session_id,
                        actor,
                        resume=bool(payload.get("resume", False)),
                    )
                    if not result["started"]:
                        raise ValueError(
                            "；".join(result.get("blockers") or ["准备未完成"])
                        )
                    await self.database.resume_session_timers(
                        session_id,
                        actor,
                    )
                    session = result["session"]
                else:
                    if action == "pause":
                        await self.database.pause_session_timers(
                            session_id,
                            actor,
                        )
                    if action in {"finish", "abort"}:
                        session = await self.database.finalize_session(
                            session_id,
                            actor,
                            termination_type=(
                                "aborted"
                                if action == "abort"
                                else "completed"
                            ),
                            reason=str(
                                payload.get("reason")
                                or (
                                    "正常完结"
                                    if action == "finish"
                                    else ""
                                )
                            ),
                        )
                        await self.broker.publish(
                            {
                                "type": "session",
                                "action": action,
                                "session_id": session["id"],
                            }
                        )
                        return json_response({"session": session})
                    target_map = {
                        "start": SESSION_PREPARING,
                        "resume": SESSION_PREPARING,
                        "pause": SESSION_PAUSED,
                        "close": SESSION_CLOSED,
                        "maintenance": SESSION_MAINTENANCE,
                    }
                    if action not in target_map:
                        raise ValueError("不支持的会话操作")
                    session = await self.database.transition_session(
                        session_id,
                        target_map[action],
                        actor,
                        str(payload.get("world_ref") or ""),
                    )
            await self.broker.publish(
                {
                    "type": "session",
                    "action": action,
                    "session_id": session["id"],
                }
            )
            return json_response({"session": session})
        except Exception as exc:
            return self._handle_error(exc)

    async def session_state(self):
        try:
            payload = await self._payload()
            state = payload.get("world_state")
            if not isinstance(state, dict):
                raise ValueError("world_state 必须是 JSON 对象")
            session = await self.database.save_manual_state(
                str(payload.get("session_id") or ""),
                state,
                int(payload.get("revision")),
                self._actor(),
            )
            await self.broker.publish(
                {
                    "type": "session",
                    "action": "state_edit",
                    "session_id": session["id"],
                }
            )
            return json_response({"session": session})
        except Exception as exc:
            return self._handle_error(exc)

    async def session_turn_order(self):
        try:
            payload = await self._payload()
            order = payload.get("order")
            if not isinstance(order, list):
                raise ValueError("order 必须是用户 ID 数组")
            turn = await self.database.set_turn_order(
                str(payload.get("session_id") or ""),
                [str(item) for item in order],
                self._actor(),
            )
            await self.broker.publish(
                {
                    "type": "session",
                    "action": "turn_order",
                    "session_id": str(payload.get("session_id") or ""),
                }
            )
            return json_response({"turn": turn})
        except Exception as exc:
            return self._handle_error(exc)

    async def session_time_rules(self):
        try:
            payload = await self._payload()
            rules = payload.get("rules")
            if not isinstance(rules, dict):
                raise ValueError("rules 必须是 JSON 对象")
            item = await self.database.save_instance_time_rules(
                str(payload.get("session_id") or ""),
                rules,
                self._actor(),
            )
            await self.broker.publish(
                {
                    "type": "timing",
                    "action": "rules_update",
                    "session_id": item["session_id"],
                }
            )
            return json_response({"instance_config": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def session_rules(self):
        try:
            payload = await self._payload()
            rules = payload.get("rules")
            if not isinstance(rules, dict):
                raise ValueError("rules 必须是 JSON 对象")
            item = await self.database.save_session_rule_state(
                str(payload.get("session_id") or ""),
                rules,
                self._actor(),
            )
            await self.broker.publish(
                {
                    "type": "session",
                    "action": "rules_update",
                    "session_id": item["session_id"],
                }
            )
            return json_response({"rule_state": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def session_npc(self):
        try:
            payload = await self._payload()
            item = await self.database.save_session_character(
                payload,
                self._actor(),
            )
            await self.broker.publish(
                {
                    "type": "session",
                    "action": "npc_update",
                    "session_id": item["session_id"],
                }
            )
            return json_response({"character": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def session_timer(self):
        try:
            payload = await self._payload()
            item = await self.database.control_timer(
                str(payload.get("timer_id") or ""),
                str(payload.get("action") or ""),
                self._actor(),
                seconds=int(payload.get("seconds") or 0),
            )
            await self.broker.publish(
                {
                    "type": "timing",
                    "action": str(payload.get("action") or ""),
                    "session_id": item["session_id"],
                }
            )
            return json_response({"timer": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def session_timer_policy(self):
        try:
            payload = await self._payload()
            item = await self.database.set_timer_policy(
                str(payload.get("session_id") or ""),
                str(payload.get("timer_type") or "all"),
                bool(payload.get("enabled", False)),
                self._actor(),
            )
            await self.broker.publish(
                {
                    "type": "timing",
                    "action": "policy",
                    "session_id": item["session_id"],
                }
            )
            return json_response({"policy": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def session_token_quota(self):
        try:
            payload = await self._payload()
            item = await self.database.set_token_quota(
                str(payload.get("session_id") or ""),
                str(payload.get("scope_type") or "session"),
                window_seconds=int(payload.get("window_seconds") or 3600),
                token_limit=int(payload.get("token_limit") or 100000),
                enabled=bool(payload.get("enabled", True)),
                actor_id=self._actor(),
            )
            await self.broker.publish(
                {
                    "type": "token",
                    "action": "quota",
                    "session_id": item["session_id"],
                }
            )
            return json_response({"usage": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def session_card_review(self):
        try:
            payload = await self._payload()
            item = await self.database.review_character_card(
                str(payload.get("session_id") or ""),
                str(payload.get("participant_ref") or ""),
                bool(payload.get("approved", False)),
                self._actor(),
                str(payload.get("note") or ""),
            )
            await self.broker.publish(
                {
                    "type": "card",
                    "action": "review",
                    "session_id": item["session_id"],
                }
            )
            return json_response({"participant": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def session_permission(self):
        try:
            payload = await self._payload()
            item = await self.database.grant_permission(
                str(payload.get("session_id") or ""),
                str(payload.get("user_id") or ""),
                str(payload.get("role") or ""),
                self._actor(),
            )
            return json_response({"permission": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def session_participant(self):
        try:
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            participant_ref = str(payload.get("participant_ref") or "")
            action = str(payload.get("action") or "").strip().lower()
            actor = self._actor()
            if action == "retire":
                result = await self.database.retire_participant(
                    session_id,
                    participant_ref,
                    actor,
                    forced=True,
                    reason=str(payload.get("reason") or "web_retire"),
                )
            elif action == "ban":
                duration = payload.get("duration_seconds")
                result = await self.database.create_ban(
                    session_id,
                    participant_ref,
                    actor,
                    scope=str(payload.get("scope") or "instance"),
                    duration_seconds=(
                        int(duration) if duration not in {None, ""} else None
                    ),
                    reason=str(payload.get("reason") or ""),
                )
            elif action == "unban":
                result = {
                    "revoked": await self.database.revoke_ban(
                        session_id,
                        participant_ref,
                        actor,
                    )
                }
            elif action == "designate":
                participant = await self.database.get_participant(
                    session_id,
                    participant_ref=participant_ref,
                )
                result = await self.database.designate_turn(
                    session_id,
                    participant["group_user_id"],
                    actor,
                )
            else:
                raise ValueError("不支持的参与者操作")
            await self.broker.publish(
                {
                    "type": "participant",
                    "action": action,
                    "session_id": session_id,
                }
            )
            return json_response({"result": result})
        except Exception as exc:
            return self._handle_error(exc)

    async def players(self):
        try:
            self._username()
            session_id = str(request.query.get("session_id", "") or "")
            return json_response(
                {"items": await self.database.list_players(session_id)}
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def player_save(self):
        try:
            item = await self.database.save_player(
                await self._payload(),
                self._actor(),
            )
            return json_response({"item": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def player_delete(self):
        try:
            payload = await self._payload()
            await self.database.delete_player(
                str(payload.get("id", "")),
                self._actor(),
            )
            return json_response({"deleted": True})
        except Exception as exc:
            return self._handle_error(exc)

    async def memories(self):
        try:
            self._username()
            session_id = str(request.query.get("session_id", "") or "")
            query = str(request.query.get("q", "") or "")
            limit = request.query.get("limit", 100, type=int)
            return json_response(
                {
                    "items": await self.database.list_memories(
                        session_id,
                        query,
                        limit,
                    )
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def memory_save(self):
        try:
            item = await self.database.save_memory(
                await self._payload(),
                self._actor(),
            )
            return json_response({"item": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def memory_delete(self):
        try:
            payload = await self._payload()
            await self.database.delete_memory(
                str(payload.get("id", "")),
                self._actor(),
            )
            return json_response({"deleted": True})
        except Exception as exc:
            return self._handle_error(exc)

    async def snapshots(self):
        try:
            self._username()
            session_id = str(request.query.get("session_id", "") or "")
            return json_response(
                {"items": await self.database.list_snapshots(session_id)}
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def snapshot_create(self):
        try:
            payload = await self._payload()
            item = await self.database.create_snapshot(
                str(payload.get("session_id") or ""),
                str(payload.get("name") or ""),
                self._actor(),
                bool(payload.get("replace", False)),
            )
            return json_response({"item": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def snapshot_restore(self):
        try:
            payload = await self._payload()
            item = await self.database.restore_snapshot(
                str(payload.get("session_id") or ""),
                str(payload.get("snapshot_ref") or ""),
                self._actor(),
            )
            await self.database.pause_session_timers(
                str(payload.get("session_id") or ""),
                self._actor(),
            )
            await self.broker.publish(
                {
                    "type": "session",
                    "action": "restore",
                    "session_id": item["id"],
                }
            )
            return json_response({"session": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def snapshot_delete(self):
        try:
            payload = await self._payload()
            await self.database.delete_snapshot(
                str(payload.get("id", "")),
                self._actor(),
            )
            return json_response({"deleted": True})
        except Exception as exc:
            return self._handle_error(exc)

    async def archive_delete(self):
        try:
            payload = await self._payload()
            item = await asyncio.to_thread(
                self.database.storage.trash_archive,
                str(payload.get("session_id") or ""),
                kind=str(payload.get("kind") or "save"),
                filename=str(payload.get("filename") or ""),
            )
            await self.broker.publish(
                {
                    "type": "storage",
                    "action": "archive_delete",
                    "session_id": item["session_id"],
                }
            )
            return json_response({"item": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def audit(self):
        try:
            self._username()
            return json_response(
                {
                    "items": await self.database.list_audit(
                        str(request.query.get("session_id", "") or ""),
                        request.query.get("limit", 100, type=int),
                        request.query.get("offset", 0, type=int),
                    )
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def providers(self):
        try:
            self._username()
            getter = getattr(self.context, "get_all_providers", None)
            providers = getter() if callable(getter) else []
            items: list[dict[str, str]] = []
            seen: set[str] = set()
            for provider in providers or []:
                meta_getter = getattr(provider, "meta", None)
                meta = meta_getter() if callable(meta_getter) else None
                meta_id = (
                    meta.get("id", "")
                    if isinstance(meta, dict)
                    else getattr(meta, "id", "")
                )
                provider_id = str(
                    meta_id
                    or getattr(provider, "id", "")
                    or ""
                ).strip()
                if not provider_id or provider_id in seen:
                    continue
                seen.add(provider_id)
                meta_name = (
                    meta.get("name", "")
                    if isinstance(meta, dict)
                    else getattr(meta, "name", "")
                )
                provider_name = (
                    meta.get("provider_name", "")
                    if isinstance(meta, dict)
                    else getattr(meta, "provider_name", "")
                )
                name = str(
                    meta_name
                    or provider_name
                    or provider_id
                ).strip()
                meta_model = (
                    meta.get("model", "")
                    if isinstance(meta, dict)
                    else getattr(meta, "model", "")
                )
                meta_model_name = (
                    meta.get("model_name", "")
                    if isinstance(meta, dict)
                    else getattr(meta, "model_name", "")
                )
                model = str(
                    meta_model
                    or meta_model_name
                    or getattr(provider, "model_name", "")
                    or ""
                ).strip()
                items.append(
                    {
                        "id": provider_id,
                        "name": name,
                        "model": model,
                    }
                )
            return json_response({"items": items})
        except Exception as exc:
            return self._handle_error(exc)

    async def settings(self):
        try:
            self._username()
            config = TavernConfig.from_mapping(self.plugin_config)
            settings = config.to_mapping()
            revision = await self.database.record_configuration_revision(
                settings,
                self._actor(),
            )
            return json_response(
                {
                    "settings": settings,
                    "config_state": revision,
                    "provider_health": (
                        await self.database.list_provider_health()
                    ),
                    "readiness": {
                        "has_admin": bool(config.admin_ids),
                        "has_allowed_group": bool(config.allowed_group_ids)
                        or not config.require_group_whitelist,
                    },
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def settings_save(self):
        try:
            payload = await self._payload()
            self._username()
            normalized = TavernConfig.from_mapping(payload).to_mapping()
            default_world = await self.database.get_world(
                normalized["runtime"]["default_world_slug"]
            )
            if default_world["archived"]:
                raise ValueError("默认世界不能使用已归档世界")
            async with self.config_lock:
                missing = object()
                previous = {
                    section: self.plugin_config.get(section, missing)
                    for section in normalized
                }
                try:
                    for section, value in normalized.items():
                        self.plugin_config[section] = value
                    save_async = getattr(
                        self.plugin_config,
                        "save_config_async",
                        None,
                    )
                    if callable(save_async):
                        await save_async()
                    else:
                        save = getattr(
                            self.plugin_config,
                            "save_config",
                            None,
                        )
                        if callable(save):
                            save()
                except Exception:
                    for section, value in previous.items():
                        if value is missing:
                            self.plugin_config.pop(section, None)
                        else:
                            self.plugin_config[section] = value
                    raise
            persisted = TavernConfig.from_mapping(
                self.plugin_config
            ).to_mapping()
            if persisted != normalized:
                raise RuntimeError("配置保存后回读校验不一致")
            revision = await self.database.record_configuration_revision(
                persisted,
                self._actor(),
            )
            await self.database.write_audit(
                "",
                self._actor(),
                "settings.update",
                "plugin",
                {
                    "admin_count": len(
                        normalized["security"]["admin_ids"]
                    ),
                    "group_count": len(
                        normalized["security"]["allowed_group_ids"]
                    ),
                },
            )
            await self.broker.publish(
                {"type": "settings", "action": "update"}
            )
            return json_response(
                {
                    "settings": persisted,
                    "config_state": revision,
                    "provider_health": (
                        await self.database.list_provider_health()
                    ),
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def backup_export(self):
        try:
            self._username()
            bundle = await self.database.export_bundle()
            export_dir = self.data_dir / "exports"
            export_dir.mkdir(parents=True, exist_ok=True)
            path = next_timestamped_path(
                export_dir,
                "backup_tavern",
                ".zip",
            )
            temporary = path.with_name(
                f".{path.name}.{uuid.uuid4().hex}.tmp"
            )
            catalog_copy = export_dir / (
                f".catalog.{uuid.uuid4().hex}.sqlite3"
            )
            bundle_bytes = (
                json.dumps(
                    bundle,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")
            checksum_lines = [
                (
                    hashlib.sha256(bundle_bytes).hexdigest(),
                    "bundle.json",
                )
            ]
            try:
                with closing(
                    sqlite3.connect(self.database.path)
                ) as source:
                    with closing(sqlite3.connect(catalog_copy)) as target:
                        source.backup(target)
                checksum_lines.append(
                    (
                        file_sha256(catalog_copy),
                        "catalog.sqlite3",
                    )
                )
                group_files: list[tuple[Path, str]] = []
                groups_dir = self.data_dir / "groups"
                if groups_dir.exists():
                    for item in sorted(groups_dir.rglob("*")):
                        if (
                            not item.is_file()
                            or item.is_symlink()
                            or item.name.endswith(("-wal", "-shm", ".tmp"))
                            or item.name.startswith(".")
                        ):
                            continue
                        archive_name = item.relative_to(
                            self.data_dir
                        ).as_posix()
                        group_files.append((item, archive_name))
                with zipfile.ZipFile(
                    temporary,
                    "w",
                    compression=zipfile.ZIP_DEFLATED,
                    compresslevel=6,
                ) as archive:
                    archive.writestr("bundle.json", bundle_bytes)
                    archive.write(catalog_copy, "catalog.sqlite3")
                    for item, archive_name in group_files:
                        digest = hashlib.sha256()
                        with item.open("rb") as source, archive.open(
                            archive_name,
                            "w",
                            force_zip64=True,
                        ) as output:
                            for chunk in iter(
                                lambda: source.read(1024 * 1024),
                                b"",
                            ):
                                digest.update(chunk)
                                output.write(chunk)
                        checksum_lines.append(
                            (digest.hexdigest(), archive_name)
                        )
                    archive.writestr(
                        "checksum.sha256",
                        "".join(
                            f"{digest}  {name}\n"
                            for digest, name in checksum_lines
                        ).encode("utf-8"),
                    )
                replace_with_retry(temporary, path)
            finally:
                unlink_with_retry(temporary, suppress_errors=True)
                unlink_with_retry(catalog_copy, suppress_errors=True)
            return file_response(
                path,
                filename=path.name,
                content_type="application/zip",
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def backup_import(self, mode: str):
        temp_path: Path | None = None
        stage_dir: Path | None = None
        staged_group_files: list[tuple[Path, PurePosixPath]] = []
        try:
            self._username()
            if mode not in {"merge", "replace"}:
                raise ValueError("导入模式必须为 merge 或 replace")
            files = await request.files()
            upload = files.get("file")
            if not isinstance(upload, PluginUploadFile):
                raise ValueError("缺少备份文件")
            filename = str(upload.filename or "").lower()
            if not filename.endswith((".json", ".zip")):
                raise ValueError("只接受完整的 Schema 9 或 Schema 10 JSON/ZIP 备份")
            temp_dir = self.data_dir / "imports"
            temp_dir.mkdir(parents=True, exist_ok=True)
            suffix = ".zip" if filename.endswith(".zip") else ".json"
            temp_path = temp_dir / f"{uuid.uuid4().hex}{suffix}"
            await upload.save(temp_path)
            limit = 512 * 1024 * 1024 if suffix == ".zip" else 25 * 1024 * 1024
            if temp_path.stat().st_size > limit:
                raise ValueError(
                    "ZIP 备份不能超过 512 MiB"
                    if suffix == ".zip"
                    else "JSON 备份不能超过 25 MiB"
                )
            if suffix == ".zip":
                with zipfile.ZipFile(temp_path) as archive:
                    _verify_backup_archive(archive)
                    try:
                        info = archive.getinfo("bundle.json")
                    except KeyError as exc:
                        raise ValueError(
                            "ZIP 备份缺少 bundle.json"
                        ) from exc
                    if info.file_size > 25 * 1024 * 1024:
                        raise ValueError("ZIP 内的 bundle.json 过大")
                    bundle = json.loads(
                        archive.read(info).decode("utf-8")
                    )
                    stage_dir = temp_dir / f".stage-{uuid.uuid4().hex}"
                    stage_dir.mkdir(parents=True)
                    staged_group_files = _stage_group_files(
                        archive,
                        stage_dir,
                    )
            else:
                bundle = json.loads(temp_path.read_text(encoding="utf-8"))
            if not isinstance(bundle, dict):
                raise ValueError("备份根节点必须是 JSON 对象")
            counts = await self.database.import_bundle(
                bundle,
                mode,
                self._actor(),
            )
            groups_root = (self.data_dir / "groups").resolve()
            for staged, relative in staged_group_files:
                destination = self.data_dir.joinpath(
                    *relative.parts
                ).resolve()
                if (
                    destination != groups_root
                    and groups_root not in destination.parents
                ):
                    raise ValueError("ZIP 备份含非法群目录路径")
                is_archive = any(
                    part in {"saves", "backups"}
                    for part in relative.parts
                )
                # Active databases and manifests have just been rebuilt from
                # bundle.json by TavernDatabase.import_bundle(). Restoring an
                # older physical copy over them could desynchronise the
                # catalog, so only immutable save/backup archives are copied
                # from the ZIP.
                if not is_archive:
                    continue
                if mode == "merge" and destination.exists():
                    if file_sha256(destination) == file_sha256(staged):
                        continue
                    destination = _collision_path(destination)
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staged, destination)
            await self.broker.publish(
                {"type": "backup", "action": "import", "mode": mode}
            )
            return json_response({"imported": counts})
        except json.JSONDecodeError:
            return error_response("备份 JSON 无法解析", status_code=400)
        except UnicodeDecodeError:
            return error_response("备份文本编码必须为 UTF-8", status_code=400)
        except zipfile.BadZipFile:
            return error_response("ZIP 备份已损坏或格式无效", status_code=400)
        except Exception as exc:
            return self._handle_error(exc)
        finally:
            if temp_path and temp_path.exists():
                temp_path.unlink(missing_ok=True)
            if stage_dir and stage_dir.exists():
                shutil.rmtree(stage_dir, ignore_errors=True)

    async def events(self):
        try:
            self._username()

            async def stream():
                async for item in self.broker.subscribe():
                    yield "data: " + json.dumps(
                        item,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ) + "\n\n"

            return stream_response(stream())
        except Exception as exc:
            return self._handle_error(exc)
