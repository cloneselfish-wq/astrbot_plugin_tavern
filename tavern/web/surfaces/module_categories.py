from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


CATEGORY_SPECS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "narrative",
        "叙事",
        "故事规划、回合状态、模型生成、主持控制与计时。",
        ("core", "state_machine", "ai_pipeline", "prompts", "model_parser", "human_dm", "delegation", "timers"),
    ),
    (
        "rules",
        "规则",
        "权限边界、角色资源、经济与关系规则。",
        ("permissions", "resources", "relationships"),
    ),
    (
        "world",
        "世界",
        "世界扩展、存档恢复与完整备份。",
        ("saves", "backups", "extensions"),
    ),
    (
        "storage",
        "存储",
        "数据库、配置、审计诊断与统一错误记录。",
        ("database", "configuration", "audit", "errors"),
    ),
    (
        "delivery",
        "投递",
        "AstrBot 入口、平台适配与可靠消息投递。",
        ("entrypoint", "platforms", "notifications"),
    ),
    (
        "webui",
        "WebUI",
        "Web API、控制台状态、页面交互与本地交付验证。",
        ("web_api", "web_ui", "frontend_state", "tests", "documentation", "release"),
    ),
)


def _text(value: Any, default: str = "") -> str:
    result = str(value or "").strip()
    return result or default


def _state(item: Mapping[str, Any]) -> str:
    if not bool(item.get("enabled")):
        return "已停用"
    status = _text(item.get("status")).lower()
    if status in {"failed", "error", "blocked"}:
        return "异常"
    if status in {"", "ready", "healthy"}:
        return "可用"
    return "需要关注"


def aggregate_module_catalog(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    raw_by_id = {
        _text(item.get("id")): dict(item)
        for item in rows
        if isinstance(item, Mapping) and _text(item.get("id"))
    }
    category_by_id = {
        module_id: category_key
        for category_key, _label, _summary, module_ids in CATEGORY_SPECS
        for module_id in module_ids
    }
    label_by_id = {
        module_id: _text(item.get("label"), "模块名称缺失")
        for module_id, item in raw_by_id.items()
    }
    categories: list[dict[str, Any]] = []
    for category_key, label, summary, module_ids in CATEGORY_SPECS:
        members = [raw_by_id[module_id] for module_id in module_ids if module_id in raw_by_id]
        member_ids = {module_id for module_id in module_ids if module_id in raw_by_id}
        external_dependencies = {
            category_by_id.get(_text(dependency))
            for item in members
            for dependency in item.get("dependencies") or ()
            if _text(dependency) not in member_ids
        }
        external_consumers = {
            category_by_id.get(_text(consumer))
            for item in members
            for consumer in item.get("consumers") or ()
            if _text(consumer) not in member_ids
        }
        states = [_state(item) for item in members]
        status = (
            "error"
            if "异常" in states
            else "attention"
            if any(state != "可用" for state in states)
            else "ready"
        )
        changed_at = max((_text(item.get("changed_at")) for item in members), default="")
        registry = [
            {
                "label": label_by_id[module_id],
                "summary": _text(raw_by_id[module_id].get("description"), "职责说明暂不可用。"),
                "state": _state(raw_by_id[module_id]),
                "layer": _text(raw_by_id[module_id].get("layer"), "unknown"),
                "dependencies": [
                    label_by_id[value]
                    for value in (_text(item) for item in raw_by_id[module_id].get("dependencies") or ())
                    if value in label_by_id
                ],
                "consumers": [
                    label_by_id[value]
                    for value in (_text(item) for item in raw_by_id[module_id].get("consumers") or ())
                    if value in label_by_id
                ],
            }
            for module_id in module_ids
            if module_id in raw_by_id
        ]
        categories.append(
            {
                "id": category_key,
                "label": label,
                "description": summary,
                "enabled": bool(members) and any(bool(item.get("enabled")) for item in members),
                "status": status,
                "layer": next((_text(item.get("layer")) for item in members if _text(item.get("layer"))), "unknown"),
                "layer_keys": sorted({_text(item.get("layer")) for item in members if _text(item.get("layer"))}),
                "dependencies": sorted(value for value in external_dependencies if value),
                "consumers": sorted(value for value in external_consumers if value),
                "changed_at": changed_at,
                "registry": registry,
                "can_disable": False,
            }
        )
    return categories


__all__ = ["CATEGORY_SPECS", "aggregate_module_catalog"]
