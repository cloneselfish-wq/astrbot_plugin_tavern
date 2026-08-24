"""D1 candidate bundles: logical batches and physical parts.

The bundle separates two layers that must stay distinct:

- **logical batches** face the player: they decide the ordinal ranges and
  the “第几批” numbering.  The batch size comes from the field's existing
  ``page_size`` declaration (default 5, contract range 5–10; the last batch
  may be shorter and fields with fewer candidates than the batch size are
  delivered in a single short batch);
- **physical parts** face the platform: they only split a logical batch's
  text when it exceeds the platform message-length capability.  Splitting
  never changes the logical batch, its ordinals, or its “第几批” identity.

All player-facing candidate copy comes from the shared DTO module
``tavern.copy.candidate``; platform length/quantity capabilities come from
``tavern.platform_delivery`` (single source).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from .card_wizard import (
    candidate_input_fingerprint,
    field_visible,
    preset_options,
    resolve_current_wizard_step,
)
from .copy.candidate import (
    CandidateCopyError,
    truncate_text,
)
from .copy.entities import decorate_entity
from .contracts.web_views.player_choice import project_player_choice_views
from .platform_delivery import capabilities_for


WIZARD_DELIVERY_KEY = "_wizard_delivery"

BUNDLE_SCHEMA = "tavern-candidate-bundle/1.0.0-rc10"
DELIVERY_SCHEMA = "tavern-candidate-delivery/1.0.0-rc10"

DEFAULT_BATCH_SIZE = 5
BATCH_SIZE_MIN = 5
BATCH_SIZE_MAX = 10
LONG_LIST_THRESHOLD = 20


def delivery_batch_size(
    field: Mapping[str, Any],
    candidate_count: int = 0,
    *,
    strict: bool = False,
) -> int:
    """Resolve the logical batch size from the field's ``page_size``.

    The declared value is the single source of truth (mirroring
    ``card_wizard.page_size``, clamped into 1–10).  With ``strict=True`` the
    declared value must also satisfy the 5–10 delivery contract whenever the
    field has enough candidates to fill a batch; the shipped worlds still
    declare 3–4 on a few dependent fields, so the bundle builder itself runs
    non-strict and the strict check is reserved for the authoring preflight.
    """

    declared = field.get("page_size", DEFAULT_BATCH_SIZE)
    try:
        raw = int(declared if declared is not None else DEFAULT_BATCH_SIZE)
    except (TypeError, ValueError):
        raw = DEFAULT_BATCH_SIZE
    if (
        strict
        and int(candidate_count or 0) >= BATCH_SIZE_MIN
        and not BATCH_SIZE_MIN <= raw <= BATCH_SIZE_MAX
    ):
        raise ValueError(
            f"字段「{field.get('label') or field.get('key') or '?'}」的批次大小 "
            f"{raw} 超出 {BATCH_SIZE_MIN}–{BATCH_SIZE_MAX}，请调整 page_size。"
        )
    return max(1, min(BATCH_SIZE_MAX, raw))


def _digest(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _step_instruction(step: Mapping[str, Any], field_type: str) -> str:
    if field_type == "multi_select":
        minimum = max(0, int(step.get("min_choices", 0) or 0))
        maximum = max(minimum, int(step.get("max_choices", 100) or 100))
        if minimum == maximum:
            return (
                f"本项目必须选择 {minimum} 个，可用逗号或空格分隔全局序号。\n"
                "示例：1, 4, 7"
            )
        return (
            f"本项目请选择 {minimum}–{maximum} 个，可用逗号或空格分隔全局序号。\n"
            "示例：1, 4, 7"
        )
    return "请回复一个全局序号，或输入完整名称。"


def _candidate_step_title(
    template: Mapping[str, Any],
    values: Mapping[str, Any],
    *,
    source_index: int | None,
    step_kind: str,
    label: str,
) -> str:
    """Build the visible wizard progress title carried by every batch."""

    if step_kind == "synthetic" or source_index is None:
        return label
    definitions = [
        item
        for item in template.get("fields") or []
        if isinstance(item, Mapping)
    ]
    index = max(0, min(int(source_index), max(0, len(definitions) - 1)))
    from .lifecycle import field_stage, staged_creation

    if not staged_creation(template):
        return f"角色卡 {index + 1}/{len(definitions)}｜{label}"
    target_stage = field_stage(definitions[index])
    visible_positions = [
        position
        for position, item in enumerate(definitions)
        if field_stage(item) == target_stage and field_visible(item, values)
    ]
    position = (
        visible_positions.index(index) + 1
        if index in visible_positions
        else sum(1 for item_index in visible_positions if item_index < index) + 1
    )
    return (
        f"角色卡 {target_stage} 组 {position}/{len(visible_positions)}｜{label}"
    )


def _render_logical_batch(
    step: Mapping[str, Any],
    choices: Sequence[Mapping[str, Any]],
    *,
    batch_index: int,
    batch_count: int,
    start: int,
    end: int,
    field_type: str,
    long_list: bool,
) -> str:
    label = str(
        step.get("delivery_title")
        or step.get("label")
        or step.get("key")
        or "候选"
    )
    lines = [
        f"【{label}】",
        f"第 {batch_index + 1}/{batch_count} 批｜全局序号 {start}—{end}",
        "",
    ]
    if batch_index == 0:
        lines.append(_step_instruction(step, field_type))
        lines.append("")
        if long_list:
            lines.append("列表较长，发送「查看选项 <序号>」可查看单项完整内容。")
            lines.append("")
    for ordinal, choice in enumerate(choices, start=start):
        compatibility = choice.get("compatibility")
        compatibility = (
            compatibility if isinstance(compatibility, Mapping) else {}
        )
        compatibility_label = str(compatibility.get("label") or "")
        label = decorate_entity(
            str(choice.get("entity_type") or ""),
            str(choice.get("label") or ""),
        )
        lines.append(
            f"〔{ordinal}〕 {label}"
            + (f"｜{compatibility_label}" if compatibility_label == "推荐" else "")
        )
        summary = str(choice.get("summary") or "")
        if summary:
            lines.append(summary)
        mechanical_preview = choice.get("mechanical_preview") or []
        for item in mechanical_preview:
            text = str(item or "").strip()
            if text:
                lines.append(text)
        if not long_list:
            advantages = choice.get("advantages") or []
            limitations = choice.get("limitations") or []
            if advantages:
                lines.append("优势｜" + "；".join(str(item) for item in advantages[:1]))
            if limitations:
                lines.append("限制｜" + "；".join(str(item) for item in limitations[:1]))
        lines.append("")
    if batch_index == batch_count - 1:
        lines.append("全部候选已发送，请按全局序号作答。")
    else:
        lines.append("下一批候选将陆续发送。")
    return "\n".join(lines)


def _split_physical(text: str, maximum: int) -> list[str]:
    """Split one logical batch into platform-sized parts by entry blocks.

    Every resulting part keeps the batch header (title + “第几批” line) so
    players always see the batch identity; ``_mark_segment`` then appends
    the “（第 i/j 段）” marker.  Splitting never changes candidate ordinals.
    """

    value = str(text or "").strip()
    if not value:
        return []
    blocks = [block.strip() for block in value.split("\n\n") if block.strip()]
    header = "\n\n".join(blocks[:2]) if len(blocks) >= 2 else blocks[0]
    body_blocks = blocks[2:]
    limit = max(1, int(maximum or 3500))
    pages: list[str] = []
    current = ""
    for block in body_blocks:
        candidate = f"{current}\n\n{block}".strip() if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            pages.append(current)
            current = ""
        remaining = block
        while len(remaining) > limit:
            prefix = truncate_text(remaining, limit)
            if not prefix:
                prefix = remaining[:limit]
            pages.append(prefix.strip())
            remaining = remaining[len(prefix) :].strip()
        current = remaining
    if current or not pages:
        pages.append(current)
    if len(pages) == 1:
        return [value]
    return [f"{header}\n\n{page}".strip() for page in pages]


def _mark_segment(
    text: str,
    *,
    batch_index: int,
    batch_count: int,
    start: int,
    end: int,
    segment: int,
    total_segments: int,
) -> str:
    if total_segments <= 1:
        return text
    header = (
        f"第 {batch_index + 1}/{batch_count} 批 · 全局序号 {start}–{end}"
        f"（第 {segment + 1}/{total_segments} 段）"
    )
    lines = str(text).split("\n")
    for index, line in enumerate(lines):
        if line.startswith("第 ") and "批" in line:
            lines[index] = header
            break
    return "\n".join(lines)


def build_candidate_bundle(
    draft: Mapping[str, Any],
    *,
    platform_id: str = "",
) -> dict[str, Any] | None:
    """Build one immutable candidate bundle for the draft's current step.

    Raises :class:`CandidateCopyError` when the world content cannot produce
    valid player copy (empty summary for a content-bearing candidate, or a
    logical batch that cannot fit the platform burst limit); it never
    silently degrades to bare-name lists.
    """

    template = draft.get("template")
    fields = draft.get("fields")
    if not isinstance(template, Mapping) or not isinstance(fields, Mapping):
        return None
    wizard_step = resolve_current_wizard_step(
        template,
        fields,
        int(draft.get("current_step", 0) or 0),
    )
    if wizard_step is None:
        return None
    step = wizard_step.to_mapping()
    field_type = str(step.get("type") or "").lower()
    if field_type not in {"select", "preset_select", "multi_select"}:
        return None
    options = preset_options(template, step, fields)
    if not options:
        return None
    choices = project_player_choice_views(
        options,
        field=step,
        template=template,
        values=fields,
    )
    caps = capabilities_for(platform_id)
    size = delivery_batch_size(step, len(choices))
    batches = [
        choices[index : index + size]
        for index in range(0, len(choices), size)
    ]
    long_list = len(choices) > LONG_LIST_THRESHOLD
    field_label = str(step.get("label") or step.get("key") or "")
    step["delivery_title"] = _candidate_step_title(
        template,
        fields,
        source_index=wizard_step.source_index,
        step_kind=wizard_step.kind,
        label=field_label,
    )
    parts: list[dict[str, Any]] = []
    logical_batches: list[dict[str, Any]] = []
    global_part = 0
    for batch_index, batch_choices in enumerate(batches):
        start = batch_index * size + 1
        end = start + len(batch_choices) - 1
        batch_text = _render_logical_batch(
            step,
            batch_choices,
            batch_index=batch_index,
            batch_count=len(batches),
            start=start,
            end=end,
            field_type=field_type,
            long_list=long_list,
        )
        segments = _split_physical(batch_text, caps.max_text_length)
        if not segments:
            segments = [batch_text]
        if len(segments) > int(caps.max_messages):
            raise CandidateCopyError(
                f"「{field_label}」单批候选文本过长，需要 {len(segments)} 条消息，"
                f"超过平台单次 {int(caps.max_messages)} 条上限。"
            )
        batch_parts: list[dict[str, Any]] = []
        for segment_index, segment in enumerate(segments):
            text = _mark_segment(
                segment,
                batch_index=batch_index,
                batch_count=len(batches),
                start=start,
                end=end,
                segment=segment_index,
                total_segments=len(segments),
            )
            part = {
                "part": global_part,
                "logical_batch": batch_index,
                "segment": segment_index,
                "segment_count": len(segments),
                "text": text,
                "start": start,
                "end": end,
            }
            parts.append(part)
            batch_parts.append(part)
            global_part += 1
        logical_batches.append(
            {
                "logical_batch": batch_index,
                "start": start,
                "end": end,
                "parts": [item["part"] for item in batch_parts],
            }
        )
    field_key = str(step.get("key") or "")
    generation = _digest(
        {
            "schema": BUNDLE_SCHEMA,
            "field_key": field_key,
            "batch_size": size,
            "candidates": choices,
        }
    )
    candidate_ids = [str(option.get("id") or "") for option in options]
    input_fingerprint = candidate_input_fingerprint(
        step,
        fields,
        [
            dict(option.get("source") or {})
            if isinstance(option.get("source"), Mapping)
            else dict(option)
            for option in options
        ],
    )
    bundle = {
        "schema": BUNDLE_SCHEMA,
        "field_key": field_key,
        "field_label": field_label,
        "step_kind": wizard_step.kind,
        "source_index": wizard_step.source_index,
        "generation": generation,
        "platform": caps.platform,
        "platform_capabilities": {
            "platform": caps.platform,
            "max_text_length": int(caps.max_text_length),
            "max_messages": int(caps.max_messages),
            "proactive_send": bool(caps.proactive_send),
        },
        "candidate_count": len(choices),
        "batch_size": size,
        "long_list": long_list,
        "logical_batch_count": len(batches),
        "part_count": len(parts),
        "logical_batches": logical_batches,
        "parts": parts,
        "candidates": [
            {"ordinal": index, "candidate": dict(choice)}
            for index, choice in enumerate(choices, start=1)
        ],
        "candidate_snapshot": {
            "candidate_ids": candidate_ids,
            "template_revision": int(draft.get("template_revision", 0) or 0),
            "world_revision": int(draft.get("world_revision", 0) or 0),
            "input_fingerprint": input_fingerprint,
            "logical_batches": [dict(item) for item in logical_batches],
        },
    }
    bundle["digest"] = _digest(
        {key: value for key, value in bundle.items() if key != "digest"}
    )
    return bundle


def cursor_status(
    bundle: Mapping[str, Any],
    cursor: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Report whether a persisted delivery cursor still matches the bundle."""

    state = cursor if isinstance(cursor, Mapping) else {}
    if not state:
        return {"valid": True, "reason": "fresh"}
    if str(state.get("schema") or "") != DELIVERY_SCHEMA:
        return {"valid": False, "reason": "schema_legacy"}
    if str(state.get("generation") or "") != str(bundle.get("generation") or ""):
        return {"valid": False, "reason": "generation_changed"}
    if str(state.get("digest") or "") != str(bundle.get("digest") or ""):
        return {"valid": False, "reason": "digest_changed"}
    return {"valid": True, "reason": "ok"}


def pending_parts(
    bundle: Mapping[str, Any],
    cursor: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Return the physical parts still waiting for delivery.

    An invalid/legacy cursor (schema, generation or digest mismatch) resets
    the delivery to the first part; callers can use :func:`cursor_status` to
    tell the player that the candidate list was updated.
    """

    state = cursor if isinstance(cursor, Mapping) else {}
    status = cursor_status(bundle, state)
    if status["valid"]:
        start = max(0, int(state.get("next_part", 0) or 0))
    else:
        start = 0
    return [
        dict(item)
        for item in bundle.get("parts", [])
        if isinstance(item, Mapping) and int(item.get("part", 0) or 0) >= start
    ]


def _logical_batch_at(bundle: Mapping[str, Any], part: int) -> int | None:
    target = max(0, int(part))
    for item in bundle.get("parts", []):
        if not isinstance(item, Mapping):
            continue
        if int(item.get("part", 0) or 0) >= target:
            value = item.get("logical_batch")
            return int(value) if value is not None else None
    return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def delivery_state(
    bundle: Mapping[str, Any],
    *,
    next_part: int,
    status: str,
    error: str = "",
    error_code: str = "",
    failure_count: int = 0,
) -> dict[str, Any]:
    """Persist one delivery cursor."""

    part = max(0, int(next_part))
    return {
        "schema": DELIVERY_SCHEMA,
        "field_key": str(bundle.get("field_key") or ""),
        "generation": str(bundle.get("generation") or ""),
        "digest": str(bundle.get("digest") or ""),
        "next_part": part,
        "next_unsent_part": part,
        "physical_part": part,
        "logical_batch": _logical_batch_at(bundle, part),
        "total_parts": max(0, int(bundle.get("part_count", 0) or 0)),
        "status": str(status or "pending"),
        "last_error": str(error or "")[:500],
        "last_error_code": str(error_code or "")[:80],
        "failure_count": max(0, int(failure_count)),
        "updated_at": _now_iso(),
        "candidate_snapshot": {
            **dict(bundle.get("candidate_snapshot") or {}),
            "delivered_parts": part,
            "total_parts": max(0, int(bundle.get("part_count", 0) or 0)),
            "delivery_status": str(status or "pending"),
        },
    }


def candidate_detail_text(
    bundle: Mapping[str, Any],
    ordinal: int,
) -> str | None:
    """Render one candidate's full player copy for the「查看选项 <n>」command."""

    for entry in bundle.get("candidates", []):
        if not isinstance(entry, Mapping):
            continue
        if int(entry.get("ordinal", 0) or 0) != int(ordinal):
            continue
        candidate = entry.get("candidate")
        if not isinstance(candidate, Mapping):
            return None
        label = str(candidate.get("label") or "")
        entity_type = str(candidate.get("entity_type") or "")
        lines = [f"〔{int(ordinal)}〕 {decorate_entity(entity_type, label)}"]
        summary = str(candidate.get("summary") or "")
        if summary:
            lines.append(summary)
        advantages = candidate.get("advantages") or []
        limitations = candidate.get("limitations") or []
        mechanics = candidate.get("mechanical_preview") or []
        compatibility = candidate.get("compatibility")
        compatibility = (
            compatibility if isinstance(compatibility, Mapping) else {}
        )
        compatibility_label = str(compatibility.get("label") or "")
        compatibility_reasons = compatibility.get("reasons") or []
        hooks = candidate.get("story_hooks") or []
        if compatibility_label and compatibility_label != "可选":
            lines.append(f"适配状态｜{compatibility_label}")
        if compatibility_reasons:
            lines.append(
                "适配说明｜"
                + "；".join(str(item) for item in compatibility_reasons)
            )
        if mechanics:
            lines.extend(str(item) for item in mechanics if str(item).strip())
        if advantages:
            lines.append(
                "优势｜" + "；".join(str(item) for item in advantages)
            )
        if limitations:
            lines.append(
                "限制｜" + "；".join(str(item) for item in limitations)
            )
        if hooks:
            lines.append(
                "故事入口｜" + "；".join(str(item) for item in hooks)
            )
        return "\n".join(lines)
    return None


__all__ = [
    "BATCH_SIZE_MAX",
    "BATCH_SIZE_MIN",
    "BUNDLE_SCHEMA",
    "DEFAULT_BATCH_SIZE",
    "DELIVERY_SCHEMA",
    "LONG_LIST_THRESHOLD",
    "WIZARD_DELIVERY_KEY",
    "build_candidate_bundle",
    "candidate_detail_text",
    "cursor_status",
    "delivery_batch_size",
    "delivery_state",
    "pending_parts",
]

