"""B/C 补充候选生成、确认值构造与物品授予差分（纯函数）。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from ..card_wizard import (
    PRESET_REFS_KEY,
    preset_options,
    store_preset_snapshot,
    store_preset_snapshots,
)
from ..database_support import clean_card_field


DEFER_OPTION = {
    "id": "supplement:defer",
    "value": "supplement:defer",
    "label": "暂缓",
    "description": "稍后再补充，此提议将在数轮后再次出现",
}
REDUCE_OPTION = {
    "id": "supplement:reduce",
    "value": "supplement:reduce",
    "label": "降低强度",
    "description": "接受较弱的版本，不再从候选中选择",
}
FALLBACK_OPTIONS = [DEFER_OPTION, REDUCE_OPTION]

PRESET_TYPES = frozenset(
    {"preset_select", "select", "radio", "multi_select", "multi", "checkbox"}
)


def field_value_kind(field: Mapping[str, Any]) -> str:
    """字段值种类：preset（单选）/ multi（多选）/ text（自由文本）。"""

    ftype = str(field.get("type") or "").strip().lower()
    if ftype in {"textarea", "longtext"} or (
        not ftype and not field.get("options")
    ):
        return "text"
    if ftype in {"multi_select", "multi", "checkbox"}:
        return "multi"
    return "preset"


def build_candidates(
    template: Mapping[str, Any],
    field: Mapping[str, Any],
    profile: Mapping[str, Any] | None,
    *,
    rejected_ids: Sequence[str] | None = None,
    config: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """按职业/地区/身份依赖生成 3–5 个候选；拒绝过多时仅剩兜底选项。"""

    config = config if isinstance(config, Mapping) else {}
    kind = field_value_kind(field)
    if kind == "text":
        return {
            "candidates": [],
            "free_text": True,
            "pool_exhausted": False,
            "fallback": False,
        }
    rejected = {
        str(item)
        for item in (rejected_ids or [])
        if str(item or "").strip()
    }
    options = preset_options(
        template,
        field,
        profile if isinstance(profile, Mapping) else {},
    )
    available = [
        dict(option)
        for option in options
        if str(option.get("id") or "") not in rejected
    ]
    count = max(1, int(config.get("candidate_count") or 3))
    cap = max(count, int(config.get("candidate_max") or 5))
    if not available and rejected:
        return {
            "candidates": [dict(item) for item in FALLBACK_OPTIONS],
            "free_text": False,
            "pool_exhausted": True,
            "fallback": True,
        }
    picked = available[:min(count, cap)]
    return {
        "candidates": picked,
        "free_text": False,
        "pool_exhausted": bool(rejected and not picked),
        "fallback": False,
    }


def option_view(option: Mapping[str, Any]) -> dict[str, Any]:
    """玩家可见候选视图：仅序号可识别的 id、名称与一句话说明。"""

    return {
        "id": str(option.get("id") or ""),
        "label": str(option.get("label") or option.get("value") or ""),
        "description": str(option.get("description") or ""),
    }


def apply_selection(
    template: Mapping[str, Any],
    field: Mapping[str, Any],
    profile: Mapping[str, Any],
    *,
    candidates: Sequence[Mapping[str, Any]],
    candidate_ids: Sequence[str] | None = None,
    text_value: str = "",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """把玩家选择写入 profile（值与 _preset_refs 同向导一致）。"""

    profile = dict(profile)
    key = str(field.get("key") or "")
    if not key:
        raise ValueError("补充字段缺少内部键")
    kind = field_value_kind(field)
    if kind == "text":
        cleaned = clean_card_field(
            text_value,
            label=str(field.get("label") or key),
            max_chars=int(field.get("max_chars") or 500),
        )
        if not cleaned:
            raise ValueError(f"{field.get('label') or key}不能为空")
        profile[key] = cleaned
        return profile, []
    by_id = {
        str(option.get("id") or ""): dict(option)
        for option in candidates
        if str(option.get("id") or "")
    }
    ids = [str(item) for item in (candidate_ids or []) if str(item or "").strip()]
    if not ids:
        raise ValueError("请回复序号选择补充内容")
    unknown = [item for item in ids if item not in by_id]
    if unknown:
        raise ValueError("所选序号不在当前提议中，请重新查看后回复")
    chosen = [by_id[item] for item in ids]
    if kind == "multi":
        minimum = max(0, int(field.get("min_choices") or 1))
        maximum = max(minimum, int(field.get("max_choices") or 100))
        if not minimum <= len(chosen) <= maximum:
            raise ValueError(
                f"{field.get('label') or key}需选择 {minimum}–{maximum} 项，"
                "请用逗号或空格分隔序号"
            )
        profile[key] = [
            str(option.get("id") or option.get("value") or option.get("label") or "")
            for option in chosen
        ]
        store_preset_snapshots(profile, key, chosen)
        return profile, chosen
    if len(chosen) != 1:
        raise ValueError(f"{field.get('label') or key}只能选择 1 项")
    option = chosen[0]
    profile[key] = str(
        option.get("id") or option.get("value") or option.get("label") or ""
    )
    store_preset_snapshot(profile, key, option)
    return profile, chosen


def _grant_identity(grant: Mapping[str, Any]) -> tuple[str, str, str, str]:
    """物品授予去重身份（与 card_item_grants 的合并键一致）。"""

    state = grant.get("state") if isinstance(grant.get("state"), Mapping) else {}
    return (
        str(grant.get("owner_scope") or "character"),
        str(grant.get("item_id") or ""),
        str(grant.get("container") or ""),
        str(sorted(state.items())),
    )


def diff_item_grant_plans(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """仅返回新增字段带来的物品授予差额（正量）。"""

    before_rows = [
        dict(grant)
        for grant in (before.get("grants") or [])
        if isinstance(grant, Mapping)
    ]
    after_rows = [
        dict(grant)
        for grant in (after.get("grants") or [])
        if isinstance(grant, Mapping)
    ]
    counts: dict[tuple[str, str, str, str], int] = {}
    for grant in before_rows:
        counts[_grant_identity(grant)] = (
            counts.get(_grant_identity(grant), 0)
            + max(0, int(grant.get("quantity") or 0))
        )
    result: list[dict[str, Any]] = []
    for grant in after_rows:
        identity = _grant_identity(grant)
        delta = max(0, int(grant.get("quantity") or 0)) - counts.get(identity, 0)
        counts[identity] = max(0, int(grant.get("quantity") or 0))
        if delta > 0:
            item = deepcopy(grant)
            item["quantity"] = delta
            result.append(item)
    return result


__all__ = [
    "DEFER_OPTION",
    "FALLBACK_OPTIONS",
    "REDUCE_OPTION",
    "apply_selection",
    "build_candidates",
    "diff_item_grant_plans",
    "field_value_kind",
    "option_view",
]
