"""Declarative TWP extensions.

World packages may describe metrics, layout and projection aliases, but may
not provide executable adapters or expressions.  Every adapter named here is
implemented and registered by the plugin.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
import re
from typing import Any

from .constants import TWP_VERSION
from .errors import TwpPackageError, TwpValidationIssue

ALLOWED_METRIC_KINDS = frozenset(
    {"definition_count", "runtime_count", "projection_value", "constant"}
)
ALLOWED_UI_SECTIONS = frozenset(
    {
        "hero",
        "text",
        "metric_grid",
        "module_panel",
        "entity_list",
        "timeline",
        "notice",
        "party",
        "actor_detail",
        "resource_grid",
        "status_list",
        "attribute_chart",
        "inventory",
        "capability_list",
        "quest_board",
        "clock_board",
        "scene_path",
        "relationship_graph",
        "challenge_board",
        "progression_board",
    }
)
PROJECTION_ADAPTERS = frozenset(
    {
        "actor",
        "actor_fate",
        "economy",
        "faction_state",
        "knowledge_graph",
        "npc_lifecycle",
        "quest_graph",
        "scene_graph",
        "time_clock",
        "terminal_conditions",
    }
)
# The active contract accepts only the current TWP/world schema and provides no
# old-package migration adapters. A non-empty ``migrations`` declaration is
# therefore rejected by the normal allowlist check below.
MIGRATION_ADAPTERS: frozenset[str] = frozenset()
_FORBIDDEN_KEYS = frozenset(
    {
        "code",
        "script",
        "javascript",
        "python",
        "sql",
        "html",
        "css",
        "iframe",
        "url",
        "src",
        "href",
        "handler",
        "import",
        "module_path",
    }
)

_UI_SCHEMA_KEYS = frozenset(
    {
        "version",
        "density",
        "empty_policy",
        "pages",
        "party",
        "actor_detail",
        "live_lenses",
        "status_taxonomy",
        "visualizations",
        "presentation",
        "surfaces",
    }
)
_UI_PAGE_IDS = frozenset({"world_overview", "live_session", "actor_detail"})
_UI_LENS_IDS = frozenset(
    {
        "party",
        "scene",
        "quests",
        "clocks",
        "relations",
        "resources",
        "challenges",
        "tactical",
        "progression",
        "replay",
        "evidence",
        "accords",
        "assembly",
        "rumors",
        "environment",
        "elements",
    }
)
_UI_VISIBILITIES = frozenset({"public", "player", "party", "host", "admin"})
_UI_DETAIL_SECTIONS = frozenset(
    {"identity", "attributes", "resources", "statuses", "inventory", "capabilities",
     "relationships", "growth", "history"}
)
_UI_TONES = frozenset({"beneficial", "harmful", "neutral", "warning", "unknown"})
_UI_SYMBOLS = frozenset({"up", "down", "dot", "warning", "question", "clock", "shield"})
_UI_BLOCK_KINDS = frozenset(
    {"narration", "action", "dialogue", "reaction", "transition", "reveal", "system_note"}
)
_UI_SAFE_ID = re.compile(r"^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$")
_UI_SEMANTIC_ROLE = re.compile(
    r"^[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)+$"
)
_UI_FORBIDDEN_TEXT = (
    re.compile(r"[<>]"),
    re.compile(r"(?i)&(?:lt|gt|#0*60|#x0*3c);"),
    re.compile(r"(?i)(?:https?|ftp|file|javascript|data):"),
    re.compile(r"(?i)(?:www\.|//[a-z0-9]|^/(?:api|graphql|ws)(?:/|$))"),
    re.compile(r"(?i)(?:url\s*\(|@import|expression\s*\()"),
    re.compile(r"(?i)(?:\b(?:color|display|position|background|font|margin|padding)\s*:|\{[^{}]{0,200}:[^{}]{0,200}\})"),
    re.compile(r"(?i)\b(?:html|css|javascript|python|sql|selector|endpoint|handler)\b"),
    re.compile(r"(?i)\b(?:onload|onclick|onerror|function|lambda)\b"),
    re.compile(r"(?i)(?:\b(?:alert|fetch)\s*\(|\b(?:document|window|subprocess|os|sys)\.|\b(?:def|class)\s+[a-z_]\w*)"),
    re.compile(r"(?i)\b(?:select|insert|update|delete|drop|alter|create|pragma)\b.*\b(?:from|into|table|set)\b"),
    re.compile(r"(?:=>|__\w+__|\bimport\s+[a-zA-Z_])"),
)
_UI_MAX_DEPTH = 12
_UI_MAX_NODES = 4096
_VISIBILITY_ORDER = {"public": 0, "player": 1, "party": 2, "host": 3, "admin": 4}
_SOURCE_VISIBILITY = {
    "": "public",
    "public": "public",
    "player": "player",
    "character": "player",
    "party": "party",
    "group": "party",
    "host": "host",
    "dm": "host",
    "author": "admin",
    "admin": "admin",
    "private": "admin",
}
_LENS_FEATURE_DEFAULTS = {
    "scene": ("scene_graph",), "quests": ("quest_graph",), "clocks": ("time_clock",),
    "relations": ("relationship_graph",), "resources": ("resources",),
    "challenge": ("challenge_engine",), "progression": ("progression",),
}
_SECTION_FEATURE_DEFAULTS = {
    "quest_board": ("quest_graph",), "clock_board": ("time_clock",),
    "scene_path": ("scene_graph",), "relationship_graph": ("relationship_graph",),
    "challenge_board": ("challenge_engine",), "progression_board": ("progression",),
}


def _error(message: str, path: str) -> TwpPackageError:
    return TwpPackageError(
        TwpValidationIssue(
            code="protocol.manifest_invalid",
            message=message,
            path=path,
            hint="请只使用当前 TWP 声明式字段。",
        )
    )


def _sequence(value: Any, *, path: str) -> list[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise _error(f"{path} 必须是数组", path)
    return list(value)


def _safe_tree(value: Any, *, path: str = "") -> None:
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = str(raw_key).strip().lower()
            item_path = f"{path}.{raw_key}" if path else str(raw_key)
            if key in _FORBIDDEN_KEYS:
                raise _error(f"{item_path} 不允许携带可执行或远程内容", item_path)
            _safe_tree(item, path=item_path)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, item in enumerate(value):
            _safe_tree(item, path=f"{path}[{index}]")
    elif isinstance(value, float) and not math.isfinite(value):
        raise _error(f"{path or 'value'} 必须是有限数值", path or "value")


def _ui_safe_text(value: Any, path: str, *, minimum: int = 0, maximum: int = 512) -> str:
    if not isinstance(value, str):
        raise _error(f"{path} 必须是字符串", path)
    text = value.strip()
    if len(text) < minimum or len(text) > maximum:
        raise _error(f"{path} 长度必须介于 {minimum}—{maximum}", path)
    if any(ord(character) < 32 for character in text):
        raise _error(f"{path} 不允许控制字符", path)
    if any(pattern.search(text) for pattern in _UI_FORBIDDEN_TEXT):
        raise _error(f"{path} 不允许携带代码、样式、查询或远程内容", path)
    return text


def _ui_safe_tree(
    value: Any,
    *,
    path: str = "ui_schema",
    depth: int = 0,
    budget: list[int] | None = None,
) -> None:
    if budget is None:
        budget = [0]
    budget[0] += 1
    if budget[0] > _UI_MAX_NODES:
        raise _error("ui_schema 节点数量超过限制", path)
    if depth > _UI_MAX_DEPTH:
        raise _error("ui_schema 嵌套层级超过限制", path)
    if isinstance(value, Mapping):
        if len(value) > 128:
            raise _error(f"{path} 字段数量超过限制", path)
        for raw_key, item in value.items():
            if not isinstance(raw_key, str) or not raw_key or len(raw_key) > 64:
                raise _error(f"{path} 包含非法字段名", path)
            _ui_safe_tree(
                item,
                path=f"{path}.{raw_key}",
                depth=depth + 1,
                budget=budget,
            )
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if len(value) > 128:
            raise _error(f"{path} 数组长度超过限制", path)
        for index, item in enumerate(value):
            _ui_safe_tree(
                item,
                path=f"{path}[{index}]",
                depth=depth + 1,
                budget=budget,
            )
        return
    if isinstance(value, str):
        _ui_safe_text(value, path)
        return
    if isinstance(value, float) and not math.isfinite(value):
        raise _error(f"{path} 必须是有限数值", path)
    if value is not None and not isinstance(value, (bool, int, float)):
        raise _error(f"{path} 包含不支持的值类型", path)


def _ui_object(value: Any, path: str, allowed: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _error(f"{path} 必须是对象", path)
    item = dict(value)
    unknown = sorted(str(key) for key in item if key not in allowed)
    if unknown:
        raise _error(f"{path} 包含未知字段：{', '.join(unknown[:8])}", path)
    return item


def _ui_list(
    value: Any,
    path: str,
    *,
    minimum: int = 0,
    maximum: int = 128,
) -> list[Any]:
    items = _sequence(value, path=path)
    if len(items) < minimum or len(items) > maximum:
        raise _error(f"{path} 数量必须介于 {minimum}—{maximum}", path)
    return items


def _ui_choice(value: Any, path: str, choices: frozenset[str]) -> str:
    text = _ui_safe_text(value, path, minimum=1, maximum=64)
    if text not in choices:
        raise _error(f"{path} 值不受支持：{text}", path)
    return text


def _ui_identifier(value: Any, path: str, *, semantic: bool = False) -> str:
    maximum = 160 if semantic else 64
    text = _ui_safe_text(value, path, minimum=1, maximum=maximum)
    pattern = _UI_SEMANTIC_ROLE if semantic else _UI_SAFE_ID
    if pattern.fullmatch(text) is None:
        raise _error(f"{path} 不是合法声明标识", path)
    return text


def _ui_integer(value: Any, path: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _error(f"{path} 必须是整数", path)
    if value < minimum or value > maximum:
        raise _error(f"{path} 必须介于 {minimum}—{maximum}", path)
    return value


def _ui_number(value: Any, path: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _error(f"{path} 必须是数值", path)
    if isinstance(value, float) and not math.isfinite(value):
        raise _error(f"{path} 必须是有限数值", path)
    return value


def _ui_boolean(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise _error(f"{path} 必须是布尔值", path)
    return value


def _ui_features(value: Any, path: str) -> list[str]:
    result: list[str] = []
    for index, raw in enumerate(_ui_list(value, path, maximum=16)):
        feature = _ui_identifier(raw, f"{path}[{index}]")
        if feature in result:
            raise _error(f"{path} 包含重复 feature：{feature}", f"{path}[{index}]")
        result.append(feature)
    return result


def _ui_visibility(value: Any, path: str, *, default: str = "public") -> str:
    if value is None:
        return default
    return _ui_choice(value, path, _UI_VISIBILITIES)

def _validate_ui_section(
    value: Any,
    path: str,
    *,
    module_ids: set[str],
) -> dict[str, Any]:
    item = _ui_object(
        value,
        path,
        frozenset({"id", "kind", "label", "module_id", "requires", "visibility", "empty", "limit"}),
    )
    if "kind" not in item:
        raise _error(f"{path}.kind 不能为空", f"{path}.kind")
    kind = _ui_choice(item["kind"], f"{path}.kind", ALLOWED_UI_SECTIONS)
    result: dict[str, Any] = {"kind": kind}
    if "id" in item:
        result["id"] = _ui_identifier(item["id"], f"{path}.id")
    if "label" in item:
        result["label"] = _ui_safe_text(item["label"], f"{path}.label", minimum=1, maximum=80)
    if "module_id" in item:
        module_id = _ui_identifier(item["module_id"], f"{path}.module_id")
        if module_id not in module_ids:
            raise _error(f"{path}.module_id 引用了未声明模块", f"{path}.module_id")
        result["module_id"] = module_id
    result["requires"] = _ui_features(item.get("requires") or [], f"{path}.requires")
    result["visibility"] = _ui_visibility(item.get("visibility"), f"{path}.visibility")
    result["empty"] = _ui_choice(
        item.get("empty", "omit"),
        f"{path}.empty",
        frozenset({"omit", "show-empty", "show-problem-on-data-loss"}),
    )
    if "limit" in item:
        result["limit"] = _ui_integer(item["limit"], f"{path}.limit", 1, 100)
    return result

def _validate_ui_pages(
    value: Any,
    *,
    module_ids: set[str],
) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    seen_pages: set[str] = set()
    for page_index, raw_page in enumerate(
        _ui_list(value, "ui_schema.pages", minimum=1, maximum=12)
    ):
        path = f"ui_schema.pages[{page_index}]"
        page = _ui_object(raw_page, path, frozenset({"id", "label", "sections"}))
        if "id" not in page or "sections" not in page:
            raise _error(f"{path} 必须声明 id 和 sections", path)
        page_id = _ui_choice(page["id"], f"{path}.id", _UI_PAGE_IDS)
        if page_id in seen_pages:
            raise _error(f"页面重复：{page_id}", f"{path}.id")
        seen_pages.add(page_id)
        sections: list[dict[str, Any]] = []
        seen_section_ids: set[str] = set()
        for section_index, raw_section in enumerate(
            _ui_list(page["sections"], f"{path}.sections", minimum=1, maximum=24)
        ):
            section_path = f"{path}.sections[{section_index}]"
            section = _validate_ui_section(
                raw_section,
                section_path,
                module_ids=module_ids,
            )
            section_id = str(section.get("id") or "")
            if section_id and section_id in seen_section_ids:
                raise _error(f"section id 重复：{section_id}", f"{section_path}.id")
            if section_id:
                seen_section_ids.add(section_id)
            sections.append(section)
        normalized: dict[str, Any] = {"id": page_id, "sections": sections}
        if "label" in page:
            normalized["label"] = _ui_safe_text(
                page["label"], f"{path}.label", minimum=1, maximum=80
            )
        pages.append(normalized)
    return pages

def _validate_count_policy(value: Any, path: str) -> dict[str, Any]:
    item = _ui_object(value, path, frozenset({"max_compact", "detail"}))
    if "max_compact" not in item or "detail" not in item:
        raise _error(f"{path} 必须声明 max_compact 和 detail", path)
    return {
        "max_compact": _ui_integer(item["max_compact"], f"{path}.max_compact", 0, 8),
        "detail": _ui_choice(
            item["detail"],
            f"{path}.detail",
            frozenset({"none", "declared", "all-public"}),
        ),
    }

def _validate_party(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    path = "ui_schema.party"
    item = _ui_object(
        value,
        path,
        frozenset({"identity_facets", "resources", "statuses", "inventory", "capabilities", "open_detail"}),
    )
    result: dict[str, Any] = {}
    facets: list[dict[str, Any]] = []
    seen_roles: set[str] = set()
    for index, raw in enumerate(
        _ui_list(item.get("identity_facets") or [], f"{path}.identity_facets", maximum=12)
    ):
        facet_path = f"{path}.identity_facets[{index}]"
        facet = _ui_object(raw, facet_path, frozenset({"role", "label", "priority", "visibility"}))
        if not {"role", "label", "priority"} <= set(facet):
            raise _error(f"{facet_path} 必须声明 role、label、priority", facet_path)
        role = _ui_identifier(facet["role"], f"{facet_path}.role", semantic=True)
        if role in seen_roles:
            raise _error(f"identity facet role 重复：{role}", f"{facet_path}.role")
        seen_roles.add(role)
        facets.append(
            {
                "role": role,
                "label": _ui_safe_text(facet["label"], f"{facet_path}.label", minimum=1, maximum=80),
                "priority": _ui_integer(facet["priority"], f"{facet_path}.priority", 0, 1000),
                "visibility": _ui_visibility(facet.get("visibility"), f"{facet_path}.visibility"),
            }
        )
    if facets:
        result["identity_facets"] = facets
    for name in ("resources", "statuses", "inventory", "capabilities"):
        if name in item:
            result[name] = _validate_count_policy(item[name], f"{path}.{name}")
    if "open_detail" in item:
        result["open_detail"] = _ui_boolean(item["open_detail"], f"{path}.open_detail")
    else:
        result["open_detail"] = True
    return result

def _validate_actor_detail(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    path = "ui_schema.actor_detail"
    item = _ui_object(value, path, frozenset({"sections", "default_section"}))
    sections: list[str] = []
    for index, raw in enumerate(_ui_list(item.get("sections") or [], f"{path}.sections", maximum=9)):
        section = _ui_choice(raw, f"{path}.sections[{index}]", _UI_DETAIL_SECTIONS)
        if section in sections:
            raise _error(f"角色详情 section 重复：{section}", f"{path}.sections[{index}]")
        sections.append(section)
    result: dict[str, Any] = {"sections": sections}
    if "default_section" in item:
        default = _ui_choice(item["default_section"], f"{path}.default_section", _UI_DETAIL_SECTIONS)
        if default not in sections:
            raise _error("default_section 必须出现在 sections", f"{path}.default_section")
        result["default_section"] = default
    elif sections:
        result["default_section"] = sections[0]
    return result


def _validate_lenses(value: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(
        _ui_list(value, "ui_schema.live_lenses", maximum=24)
    ):
        path = f"ui_schema.live_lenses[{index}]"
        item = _ui_object(raw, path, frozenset({"id", "label", "requires", "required", "order"}))
        if "id" not in item or "label" not in item:
            raise _error(f"{path} 必须声明 id 和 label", path)
        lens_id = _ui_choice(item["id"], f"{path}.id", _UI_LENS_IDS)
        if lens_id in seen:
            raise _error(f"lens 重复：{lens_id}", f"{path}.id")
        seen.add(lens_id)
        result.append(
            {
                "id": lens_id,
                "label": _ui_safe_text(item["label"], f"{path}.label", minimum=1, maximum=80),
                "requires": _ui_features(item.get("requires") or [], f"{path}.requires"),
                "required": _ui_boolean(item.get("required", False), f"{path}.required"),
                "order": _ui_integer(item.get("order", index * 10), f"{path}.order", 0, 1000),
            }
        )
    return result


def _validate_status_taxonomy(value: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(
        _ui_list(value, "ui_schema.status_taxonomy", maximum=128)
    ):
        path = f"ui_schema.status_taxonomy[{index}]"
        item = _ui_object(raw, path, frozenset({"role", "label", "tone", "symbol", "visibility"}))
        if not {"role", "label", "tone", "symbol"} <= set(item):
            raise _error(f"{path} 必须声明 role、label、tone、symbol", path)
        role = _ui_identifier(item["role"], f"{path}.role", semantic=True)
        if role in seen:
            raise _error(f"status role 重复：{role}", f"{path}.role")
        seen.add(role)
        result.append(
            {
                "role": role,
                "label": _ui_safe_text(item["label"], f"{path}.label", minimum=1, maximum=80),
                "tone": _ui_choice(item["tone"], f"{path}.tone", _UI_TONES),
                "symbol": _ui_choice(item["symbol"], f"{path}.symbol", _UI_SYMBOLS),
                "visibility": _ui_visibility(item.get("visibility"), f"{path}.visibility"),
            }
        )
    return result


def _validate_scale(value: Any, path: str) -> dict[str, Any]:
    item = _ui_object(value, path, frozenset({"min", "max", "unit"}))
    if "min" not in item or "max" not in item:
        raise _error(f"{path} 必须声明 min 和 max", path)
    minimum = _ui_number(item["min"], f"{path}.min")
    maximum = _ui_number(item["max"], f"{path}.max")
    if minimum >= maximum:
        raise _error(f"{path}.min 必须小于 max", path)
    result: dict[str, Any] = {"min": minimum, "max": maximum}
    if "unit" in item:
        result["unit"] = _ui_safe_text(item["unit"], f"{path}.unit", maximum=24)
    return result


def _validate_visualizations(value: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(
        _ui_list(value, "ui_schema.visualizations", maximum=24)
    ):
        path = f"ui_schema.visualizations[{index}]"
        item = _ui_object(
            raw,
            path,
            frozenset({"id", "kind", "title", "roles", "scale", "fallback", "visibility"}),
        )
        if not {"id", "kind", "title", "roles", "fallback"} <= set(item):
            raise _error(f"{path} 缺少必需字段", path)
        visual_id = _ui_identifier(item["id"], f"{path}.id")
        if visual_id in seen_ids:
            raise _error(f"visualization id 重复：{visual_id}", f"{path}.id")
        seen_ids.add(visual_id)
        kind = _ui_choice(
            item["kind"],
            f"{path}.kind",
            frozenset({"bars", "radar", "segments", "timeline", "list"}),
        )
        roles: list[str] = []
        for role_index, raw_role in enumerate(
            _ui_list(item["roles"], f"{path}.roles", minimum=1, maximum=12)
        ):
            role = _ui_identifier(raw_role, f"{path}.roles[{role_index}]", semantic=True)
            if role in roles:
                raise _error(f"visualization role 重复：{role}", f"{path}.roles[{role_index}]")
            roles.append(role)
        if kind == "radar" and not 3 <= len(roles) <= 8:
            raise _error("radar 必须声明 3—8 个 role", f"{path}.roles")
        if kind == "radar" and "scale" not in item:
            raise _error("radar 必须声明统一 scale", f"{path}.scale")
        normalized: dict[str, Any] = {
            "id": visual_id,
            "kind": kind,
            "title": _ui_safe_text(item["title"], f"{path}.title", minimum=1, maximum=80),
            "roles": roles,
            "fallback": _ui_choice(
                item["fallback"], f"{path}.fallback", frozenset({"bars", "list", "table"})
            ),
            "visibility": _ui_visibility(item.get("visibility"), f"{path}.visibility"),
        }
        if "scale" in item:
            normalized["scale"] = _validate_scale(item["scale"], f"{path}.scale")
        result.append(normalized)
    return result


def _validate_presentation(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    path = "ui_schema.presentation"
    item = _ui_object(
        value,
        path,
        frozenset({"style", "preferred_block_kinds", "dialogue_density", "scene_break_label", "allow_title"}),
    )
    result: dict[str, Any] = {}
    if "style" in item:
        result["style"] = _ui_choice(
            item["style"],
            f"{path}.style",
            frozenset({"minimal", "adventure", "mystery", "dramatic", "cinematic"}),
        )
    if "preferred_block_kinds" in item:
        blocks: list[str] = []
        for index, raw in enumerate(
            _ui_list(item["preferred_block_kinds"], f"{path}.preferred_block_kinds", maximum=7)
        ):
            block = _ui_choice(raw, f"{path}.preferred_block_kinds[{index}]", _UI_BLOCK_KINDS)
            if block in blocks:
                raise _error(f"preferred block kind 重复：{block}", f"{path}.preferred_block_kinds[{index}]")
            blocks.append(block)
        result["preferred_block_kinds"] = blocks
    if "dialogue_density" in item:
        result["dialogue_density"] = _ui_choice(
            item["dialogue_density"],
            f"{path}.dialogue_density",
            frozenset({"silent", "low", "medium", "high"}),
        )
    if "scene_break_label" in item:
        result["scene_break_label"] = _ui_safe_text(
            item["scene_break_label"], f"{path}.scene_break_label", maximum=20
        )
    result["allow_title"] = _ui_boolean(item.get("allow_title", True), f"{path}.allow_title")
    return result


def validate_ui_schema(
    value: Any,
    *,
    module_ids: set[str],
) -> dict[str, Any]:
    """Validate and normalize the closed, data-only author UI contract."""

    _ui_safe_tree(value)
    item = _ui_object(value, "ui_schema", _UI_SCHEMA_KEYS)
    if "version" not in item or "pages" not in item:
        raise _error("ui_schema 必须声明 version 和 pages", "ui_schema")
    version = _ui_safe_text(item["version"], "ui_schema.version", minimum=1, maximum=64)
    if version != TWP_VERSION:
        raise _error(f"ui_schema.version 必须为 {TWP_VERSION}", "ui_schema.version")
    density = _ui_choice(
        item.get("density", "standard"),
        "ui_schema.density",
        frozenset({"minimal", "standard", "rich"}),
    )
    empty_policy = _ui_choice(
        item.get("empty_policy", "omit-unsupported"),
        "ui_schema.empty_policy",
        frozenset({"omit-unsupported"}),
    )
    from ..visualization.surface_registry import REGISTRY_VERSION, registry_entry

    surfaces: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(item.get("surfaces") or []):
        path = f"ui_schema.surfaces[{index}]"
        if not isinstance(raw, Mapping):
            raise _error(f"{path} 必须是对象", path)
        surface = dict(raw)
        surface_id = _ui_identifier(surface.get("id"), f"{path}.id")
        capability_ref = _ui_safe_text(
            surface.get("capability_ref"), f"{path}.capability_ref", minimum=1, maximum=96
        )
        module_id = _ui_safe_text(
            surface.get("module_id") or "", f"{path}.module_id", maximum=64
        )
        if surface_id in seen_ids or (module_id and module_id not in module_ids):
            raise _error(f"{path} id 重复或模块未启用", path)
        component_kind = _ui_safe_text(
            surface.get("component_kind"), f"{path}.component_kind", minimum=1, maximum=64
        )
        registry = registry_entry(component_kind)
        if registry is None:
            raise _error(f"{path} 使用未注册组件 {component_kind}", f"{path}.component_kind")
        data_kind = _ui_safe_text(
            surface.get("data_kind") or registry["data_kind"],
            f"{path}.data_kind",
            minimum=1,
            maximum=80,
        )
        if data_kind != registry["data_kind"]:
            raise _error(f"{path} component/data kind 不匹配", f"{path}.data_kind")
        placements = [
            _ui_choice(value, f"{path}.placements[{item_index}]", frozenset(registry["placements"]))
            for item_index, value in enumerate(
                _ui_list(surface.get("placements"), f"{path}.placements", minimum=1, maximum=8)
            )
        ]
        if len(set(placements)) != len(placements):
            raise _error(f"{path} placements 重复", f"{path}.placements")
        usage = _ui_choice(
            surface.get("usage"), f"{path}.usage", frozenset({"definition", "runtime", "both"})
        )
        definition_binding = _ui_object(
            surface.get("definition_binding") or {},
            f"{path}.definition_binding",
            frozenset({"projection", "required"}),
        )
        runtime_binding = _ui_object(
            surface.get("runtime_binding") or {},
            f"{path}.runtime_binding",
            frozenset({"projection", "required"}),
        )
        if usage in {"definition", "both"} and not definition_binding.get("projection"):
            raise _error(f"{path} 缺 definition projection", f"{path}.definition_binding")
        if usage in {"runtime", "both"} and not runtime_binding.get("projection"):
            raise _error(f"{path} 缺 runtime projection", f"{path}.runtime_binding")
        audience = [
            _ui_choice(value, f"{path}.audience[{item_index}]", frozenset({"player", "host", "author", "admin", "readonly", "spectator"}))
            for item_index, value in enumerate(
                _ui_list(surface.get("audience"), f"{path}.audience", minimum=1, maximum=6)
            )
        ]
        readme_sections = [
            _ui_safe_text(value, f"{path}.readme_sections[{item_index}]", minimum=1, maximum=96)
            for item_index, value in enumerate(
                _ui_list(surface.get("readme_sections"), f"{path}.readme_sections", minimum=1, maximum=16)
            )
        ]
        surface_empty_policy = _ui_choice(
            surface.get("empty_policy"),
            f"{path}.empty_policy",
            frozenset({"explicit_empty", "omit_when_not_applicable", "error_when_required"}),
        )
        required = _ui_boolean(surface.get("required", True), f"{path}.required")
        if required and surface_empty_policy == "omit_when_not_applicable":
            raise _error(f"{path} required surface 不能静默省略", f"{path}.empty_policy")
        refresh = _ui_object(
            surface.get("refresh") or {},
            f"{path}.refresh",
            frozenset({"mode", "event_types"}),
        )
        refresh_mode = _ui_choice(
            refresh.get("mode", "manual"),
            f"{path}.refresh.mode",
            frozenset({"snapshot", "revision", "manual", "sse"}),
        )
        event_types = [
            _ui_safe_text(value, f"{path}.refresh.event_types[{item_index}]", minimum=1, maximum=96)
            for item_index, value in enumerate(
                _ui_list(refresh.get("event_types") or [], f"{path}.refresh.event_types", maximum=16)
            )
        ]
        if usage in {"runtime", "both"} and refresh_mode == "sse" and not event_types:
            raise _error(f"{path} SSE surface 缺 event types", f"{path}.refresh.event_types")
        mobile = _ui_safe_text(
            surface.get("mobile_presentation") or registry["mobile"],
            f"{path}.mobile_presentation",
            minimum=1,
            maximum=64,
        )
        if mobile != registry["mobile"]:
            raise _error(f"{path} 移动端配方未注册", f"{path}.mobile_presentation")
        copy_model = _ui_object(
            surface.get("copy") or {},
            f"{path}.copy",
            frozenset({"title", "summary", "help", "impact", "boundary", "empty", "error_operation", "error_reason", "automatic_action", "recovery"}),
        )
        for copy_key in ("title", "summary", "help", "impact", "boundary", "empty", "error_operation", "error_reason", "automatic_action", "recovery"):
            copy_model[copy_key] = _ui_safe_text(
                copy_model.get(copy_key), f"{path}.copy.{copy_key}", minimum=1, maximum=500
            )
        seen_ids.add(surface_id)
        surfaces.append(
            {
                "id": surface_id,
                "capability_ref": capability_ref,
                "module_id": module_id,
                "placements": placements,
                "component_kind": component_kind,
                "data_kind": data_kind,
                "group": _ui_identifier(surface.get("group"), f"{path}.group"),
                "usage": usage,
                "definition_binding": {
                    "projection": _ui_safe_text(definition_binding.get("projection") or "", f"{path}.definition_binding.projection", maximum=96),
                    "required": bool(definition_binding.get("required", usage in {"definition", "both"})),
                },
                "runtime_binding": {
                    "projection": _ui_safe_text(runtime_binding.get("projection") or "", f"{path}.runtime_binding.projection", maximum=96),
                    "required": bool(runtime_binding.get("required", usage in {"runtime", "both"})),
                },
                "audience": audience,
                "readme_sections": readme_sections,
                "empty_policy": surface_empty_policy,
                "refresh": {"mode": refresh_mode, "event_types": event_types},
                "mobile_presentation": mobile,
                "required": required,
                "order": _ui_integer(surface.get("order"), f"{path}.order", 0, 2000),
                "visual_recipe": f"{component_kind}.standard",
                "copy": copy_model,
                "component_registry_version": REGISTRY_VERSION,
            }
        )
    return {
        "version": version,
        "density": density,
        "empty_policy": empty_policy,
        "pages": _validate_ui_pages(item["pages"], module_ids=module_ids),
        "party": _validate_party(item.get("party")),
        "actor_detail": _validate_actor_detail(item.get("actor_detail")),
        "live_lenses": _validate_lenses(item.get("live_lenses") or []),
        "status_taxonomy": _validate_status_taxonomy(item.get("status_taxonomy") or []),
        "visualizations": _validate_visualizations(item.get("visualizations") or []),
        "presentation": _validate_presentation(item.get("presentation")),
        "surfaces": sorted(
            surfaces, key=lambda value: (int(value["order"]), value["id"])
        ),
    }


def _pointer(value: Any, path: str) -> Any:
    if path in {"", "/"}:
        return value
    if not path.startswith("/"):
        raise _error("指标 path 必须是 JSON Pointer", "summary_metrics.source.path")
    current = value
    for raw in path[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            if token not in current:
                return None
            current = current[token]
        elif isinstance(current, list) and token.isdigit():
            index = int(token)
            if index >= len(current):
                return None
            current = current[index]
        else:
            return None
    return current


def validate_extensions(
    manifest: Mapping[str, Any],
    *,
    module_ids: set[str],
) -> dict[str, Any]:
    _safe_tree(
        {
            "summary_metrics": manifest.get("summary_metrics") or [],
            "ui_schema": manifest.get("ui_schema") or {},
            "projection_contracts": manifest.get("projection_contracts") or [],
            "migrations": manifest.get("migrations") or [],
            "entity_candidate_policy": (
                manifest.get("entity_candidate_policy") or {}
            ),
        }
    )
    metrics: list[dict[str, Any]] = []
    seen_metrics: set[str] = set()
    for index, raw in enumerate(
        _sequence(manifest.get("summary_metrics") or [], path="summary_metrics")
    ):
        path = f"summary_metrics[{index}]"
        if not isinstance(raw, Mapping):
            raise _error(f"{path} 必须是对象", path)
        item = dict(raw)
        metric_id = str(item.get("id") or "").strip()
        source = item.get("source")
        if (
            not metric_id
            or metric_id in seen_metrics
            or not isinstance(source, Mapping)
        ):
            raise _error(f"{path} 缺少唯一 id 或 source", path)
        kind = str(source.get("kind") or "")
        module_id = str(source.get("module_id") or "")
        if kind not in ALLOWED_METRIC_KINDS:
            raise _error(f"{path}.source.kind 不受支持", f"{path}.source.kind")
        if kind != "constant" and module_id not in module_ids:
            raise _error(f"{path} 引用了未声明模块", f"{path}.source.module_id")
        if kind != "constant" and not str(source.get("path") or "").startswith("/"):
            raise _error(f"{path}.source.path 必须是 JSON Pointer", f"{path}.source.path")
        seen_metrics.add(metric_id)
        metrics.append(item)

    ui_schema = validate_ui_schema(
        manifest.get("ui_schema") or {},
        module_ids=module_ids,
    )

    projections: list[dict[str, Any]] = []
    seen_projection_modules: set[str] = set()
    for index, raw in enumerate(
        _sequence(
            manifest.get("projection_contracts") or [],
            path="projection_contracts",
        )
    ):
        path = f"projection_contracts[{index}]"
        if not isinstance(raw, Mapping):
            raise _error(f"{path} 必须是对象", path)
        item = dict(raw)
        module_id = str(item.get("module_id") or "")
        adapter = str(item.get("adapter") or "")
        aliases = item.get("aliases") or {}
        if module_id not in module_ids or module_id in seen_projection_modules:
            raise _error(f"{path} 模块缺失、重复或未声明", f"{path}.module_id")
        if adapter not in PROJECTION_ADAPTERS:
            raise _error(f"{path}.adapter 未由插件注册", f"{path}.adapter")
        if not isinstance(aliases, Mapping):
            raise _error(f"{path}.aliases 必须是对象", f"{path}.aliases")
        seen_projection_modules.add(module_id)
        projections.append(item)

    migrations: list[dict[str, Any]] = []
    for index, raw in enumerate(
        _sequence(manifest.get("migrations") or [], path="migrations")
    ):
        path = f"migrations[{index}]"
        if not isinstance(raw, Mapping):
            raise _error(f"{path} 必须是对象", path)
        item = dict(raw)
        if str(item.get("strategy") or "") != "plugin_registered":
            raise _error(f"{path} 只允许 plugin_registered", f"{path}.strategy")
        if str(item.get("adapter") or "") not in MIGRATION_ADAPTERS:
            raise _error(f"{path}.adapter 未由插件注册", f"{path}.adapter")
        migrations.append(item)

    policy = manifest.get("entity_candidate_policy") or {}
    if not isinstance(policy, Mapping):
        raise _error("entity_candidate_policy 必须是对象", "entity_candidate_policy")
    items_policy = policy.get("items") or {}
    if items_policy and not isinstance(items_policy, Mapping):
        raise _error(
            "entity_candidate_policy.items 必须是对象",
            "entity_candidate_policy.items",
        )
    if items_policy:
        maximum = int(items_policy.get("max_candidates") or 0)
        if maximum < 1 or maximum > 80:
            raise _error(
                "实体候选上限必须介于 1—80",
                "entity_candidate_policy.items.max_candidates",
            )

    return {
        "schema": f"twp-extension-contract/{TWP_VERSION}",
        "summary_metrics": metrics,
        "ui_schema": ui_schema,
        "projection_contracts": projections,
        "migrations": migrations,
        "entity_candidate_policy": dict(policy),
    }


def compile_ui_profile(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Resolve a validated UI schema through the archive compiler registry."""

    from .references import compile_ui_profile as compile_profile

    return compile_profile(*args, **kwargs)


def compile_summary_metrics(
    metrics: Sequence[Mapping[str, Any]],
    definitions: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    compiled: list[dict[str, Any]] = []
    for raw in metrics:
        item = dict(raw)
        source = dict(item.get("source") or {})
        kind = str(source.get("kind") or "")
        value: Any = None
        if kind == "constant":
            value = source.get("value")
        elif kind == "definition_count":
            target = _pointer(
                definitions.get(str(source.get("module_id") or ""), {}),
                str(source.get("path") or ""),
            )
            value = len(target) if isinstance(target, (list, dict)) else 0
        compiled.append(
            {
                "id": str(item.get("id") or ""),
                "label": str(item.get("label") or ""),
                "value": value,
                "visibility": str(item.get("visibility") or "public"),
                "order": int(item.get("order") or 0),
                "source_kind": kind,
            }
        )
    return sorted(compiled, key=lambda row: (row["order"], row["id"]))


def validate_ai_companions(
    raw: Any,
    *,
    actor_definitions: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate a world-owned, data-only AI companion preset catalog."""
    if raw in (None, {}):
        return {
            "state": "not_applicable",
            "version": TWP_VERSION,
            "presets": [],
        }
    if not isinstance(raw, Mapping):
        raise _error("ai_companions 必须是对象", "world.ai_companions")
    value = dict(raw)
    _safe_tree(value, path="world.ai_companions")
    if not bool(value.get("enabled", True)):
        return {
            **value,
            "state": "not_applicable",
            "presets": [],
        }
    presets = _sequence(
        value.get("presets") or [],
        path="world.ai_companions.presets",
    )
    minimum = max(10, int(value.get("minimum_presets") or 10))
    if len(presets) < minimum:
        raise _error(
            f"ai_companions 至少需要 {minimum} 个预设",
            "world.ai_companions.presets",
        )
    known_refs: set[str] = set()

    def collect_ids(value: Any) -> None:
        if isinstance(value, Mapping):
            stable_id = value.get("id")
            if isinstance(stable_id, str) and stable_id:
                known_refs.add(stable_id)
            for child in value.values():
                collect_ids(child)
        elif isinstance(value, Sequence) and not isinstance(
            value, (str, bytes)
        ):
            for child in value:
                collect_ids(child)

    collect_ids(actor_definitions)
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for index, raw_preset in enumerate(presets):
        path = f"world.ai_companions.presets[{index}]"
        if not isinstance(raw_preset, Mapping):
            raise _error(f"{path} 必须是对象", path)
        preset = dict(raw_preset)
        preset_id = str(preset.get("id") or "").strip()
        if not preset_id or preset_id in seen:
            raise _error(f"{path} 缺少唯一 id", f"{path}.id")
        if not str(preset.get("name") or "").strip():
            raise _error(f"{path} 缺少本地化名称", f"{path}.name")
        if not str(preset.get("summary") or "").strip():
            raise _error(f"{path} 缺少公开简介", f"{path}.summary")
        references = [
            str(preset.get("species_ref") or ""),
            str(preset.get("profession_ref") or ""),
            *(
                str(item)
                for item in preset.get("specialization_refs") or []
            ),
            *(str(item) for item in preset.get("feat_refs") or []),
            *(str(item) for item in preset.get("goal_refs") or []),
        ]
        missing = [ref for ref in references if not ref or ref not in known_refs]
        if missing:
            raise _error(
                f"{path} 引用了不存在的角色实体",
                f"{path}.references",
            )
        seen.add(preset_id)
        normalized.append(
            {
                **preset,
                "id": preset_id,
                "version": str(preset.get("version") or TWP_VERSION),
            }
        )
    mode = str(value.get("default_mode") or "confirm")
    if mode not in {"automatic", "confirm", "paused"}:
        raise _error(
            "ai_companions.default_mode 无效",
            "world.ai_companions.default_mode",
        )
    vote_policy = str(value.get("vote_policy") or "normal")
    if vote_policy not in {"normal", "advisory", "disabled"}:
        raise _error(
            "ai_companions.vote_policy 无效",
            "world.ai_companions.vote_policy",
        )
    maximum = int(value.get("maximum_active") or 8)
    visible = int(value.get("default_visible_limit") or 3)
    if maximum < 1 or maximum > 8 or visible < 1 or visible > 3:
        raise _error(
            "AI 队友显示上限必须不超过 3，服务端上限必须不超过 8",
            "world.ai_companions.maximum_active",
        )
    return {
        **value,
        "state": "ready",
        "version": str(value.get("version") or TWP_VERSION),
        "default_mode": mode,
        "vote_policy": vote_policy,
        "maximum_active": maximum,
        "default_visible_limit": visible,
        "presets": normalized,
    }


__all__ = [
    "ALLOWED_METRIC_KINDS",
    "ALLOWED_UI_SECTIONS",
    "MIGRATION_ADAPTERS",
    "PROJECTION_ADAPTERS",
    "compile_ui_profile",
    "compile_summary_metrics",
    "validate_ai_companions",
    "validate_extensions",
    "validate_ui_schema",
]

