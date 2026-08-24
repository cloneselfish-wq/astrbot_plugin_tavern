"""D1-UX-006：WorldSummaryView 纯投影。

输入兼容两种真实形态：

- 编译后的世界快照（``world/core.json``，含 ``rules.world_stats``、
  ``rules.twp_modules``、``protocol``、``minimum_plugin_version``）；
- 数据库 ``worlds`` 行（列名与快照键一致时可直接传入）。

模块数规则（10_WEBUI_REDESIGN.md §7.1）：

- ``declared`` 优先取调用方显式计数，其次 ``rules.world_stats.protocol.modules``
  数组长度，再次 ``rules.twp_modules`` 条数；
- ``enabled`` 优先调用方显式计数，其次 ``twp_modules`` 中 enabled 条数，
  缺省回退为 ``declared``；
- 两种来源都缺失时进入 ``error`` 状态并给出“模块统计读取失败”，
  绝不输出 0 或 NaN。

普通视图不包含数据库 revision / 包 ID / slug / compiler ABI；这些只进入
``technical``（``include_technical_refs=True`` 时）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..common import clean_label, safe_int


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _module_counts(
    world: Mapping[str, Any],
    *,
    declared: int | None,
    enabled: int | None,
) -> tuple[int | None, int | None]:
    if declared is not None:
        declared_value = max(0, safe_int(declared, -1))
        enabled_value = (
            max(0, safe_int(enabled, -1))
            if enabled is not None
            else declared_value
        )
        return declared_value, enabled_value
    rules = _mapping(world.get("rules"))
    world_stats = _mapping(rules.get("world_stats"))
    modules = world_stats.get("protocol", {}).get("modules")
    if isinstance(modules, list):
        return len(modules), len(modules)
    twp_modules = rules.get("twp_modules")
    if isinstance(twp_modules, list):
        declared_value = len(twp_modules)
        enabled_value = sum(
            1
            for item in twp_modules
            if bool(_mapping(item).get("enabled"))
        )
        return declared_value, enabled_value
    return None, None


def project_world_summary_view(
    world: Mapping[str, Any],
    *,
    declared: int | None = None,
    enabled: int | None = None,
    content_stats: Mapping[str, Any] | None = None,
    include_technical_refs: bool = False,
) -> dict[str, Any]:
    """把世界数据规范化为世界卡 / 详情页共用的 WorldSummaryView。"""

    world = _mapping(world)
    rules = _mapping(world.get("rules"))
    protocol = _mapping(world.get("protocol"))
    name = clean_label(
        world.get("name") or world.get("display_name"),
        "世界名称缺失，请让管理员检查世界包。",
    )
    description = clean_label(world.get("description"))
    content_version = clean_label(
        world.get("world_content_version") or world.get("content_version")
    )
    if not content_version:
        content_version = "未提供内容版本"
    minimum_plugin_version = clean_label(world.get("minimum_plugin_version"))
    if not minimum_plugin_version:
        minimum_plugin_version = "未提供最低插件版本"
    protocol_name = clean_label(
        protocol.get("name") or world.get("protocol_name")
    )
    protocol_version = clean_label(protocol.get("version"))
    if protocol_version:
        protocol_display = (
            f"TWP {protocol_version}"
            if protocol_name.casefold() == "twp" or not protocol_name
            else f"{protocol_name} {protocol_version}"
        )
    else:
        protocol_display = "未提供协议版本"

    declared_value, enabled_value = _module_counts(
        world, declared=declared, enabled=enabled
    )
    if declared_value is None:
        module_summary = {
            "declared": None,
            "enabled": None,
            "state": "error",
            "message": "模块统计读取失败",
        }
    else:
        module_summary = {
            "declared": declared_value,
            "enabled": enabled_value,
            "state": "ready",
            "message": "",
        }

    player_limits = _mapping(
        rules.get("player_limits")
        or world.get("player_limits")
        or world.get("recommended_players")
    )
    stats = _mapping(
        content_stats
        if content_stats is not None
        else rules.get("world_stats", {}).get("content")
    )

    view: dict[str, Any] = {
        "schema": "tavern-world-summary/1.0.0-rc10",
        "name": name,
        "description": description,
        "content_version": content_version,
        "protocol_display": protocol_display,
        "minimum_plugin_version": minimum_plugin_version,
        "module_summary": module_summary,
        "player_limits": dict(player_limits) if player_limits else {},
        "player_limits_message": "" if player_limits else "未提供人数建议",
        "content_stats": dict(stats) if stats else {},
        "content_stats_message": "" if stats else "未提供统计",
        "technical": None,
    }
    if include_technical_refs:
        technical: dict[str, Any] = {
            "schema": "tavern-world-summary/1.0.0-rc10"
        }
        for key in ("slug", "package_id", "source_package_id", "revision"):
            value = world.get(key)
            if value not in (None, ""):
                technical[key] = value
        if protocol:
            technical["protocol"] = {
                key: protocol[key]
                for key in ("name", "version", "core", "compiler_abi", "maturity")
                if protocol.get(key) not in (None, "")
            }
        view["technical"] = technical
    return view


__all__ = ["project_world_summary_view"]
