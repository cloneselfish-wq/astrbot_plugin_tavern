from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


DEFAULT_SECTION_LIMITS = {
    "runtime_state": 6000,
    "world_module_runtime": 8000,
    "turn_context": 2500,
    "active_party": 20000,
    "acting_player": 2500,
    "acting_inventory": 3000,
    "shop": 1800,
    "next_actor": 2500,
    "relevant_memories": 5000,
    "recent_history": 6000,
    "active_return_requests": 2500,
    "active_npcs": 8000,
    "story_ledger": 4000,
    "scene_clocks": 4000,
    "relationship_slice": 3000,
    "content_boundaries": 2500,
    "opening_scene": 5000,
}

DEFAULT_TOTAL_CONTEXT_TOKENS = 8_000
_DEGRADE_ORDER = (
    "recent_history",
    "relevant_memories",
    "active_npcs",
    "relationship_slice",
    "world_module_runtime",
    "story_ledger",
    "scene_clocks",
)

_ACTIVE_STATUSES = {
    "",
    "active",
    "available",
    "in_progress",
    "open",
    "pending",
    "started",
    "triggered",
}


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )


def _text(value: Any) -> str:
    return str(value or "").strip()


def _bounded(value: Any, limit: int) -> Any:
    """Keep complete JSON entries while enforcing a deterministic char budget."""

    limit = max(2, int(limit))
    if len(_json(value)) <= limit:
        return value
    if isinstance(value, Mapping):
        result: OrderedDict[str, Any] = OrderedDict()
        for key in sorted(value, key=lambda item: str(item)):
            candidate = _bounded(value[key], max(64, limit // 2))
            tentative = OrderedDict(result)
            tentative[str(key)] = candidate
            if len(_json(tentative)) > limit:
                continue
            result[str(key)] = candidate
        return dict(result)
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        result: list[Any] = []
        for item in value:
            candidate = _bounded(item, max(64, limit // 2))
            if len(_json([*result, candidate])) > limit:
                break
            result.append(candidate)
        return result
    raw = _text(value)
    if len(raw) <= limit:
        return raw
    return raw[: max(0, limit - 1)].rstrip() + "…"


def _status(item: Mapping[str, Any]) -> str:
    return _text(
        item.get("status")
        or item.get("state")
        or item.get("lifecycle_status")
    ).casefold()


def _ref_tokens(value: Any) -> set[str]:
    tokens: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).endswith(("_id", "_ref", "_key")):
                text = _text(item)
                if text:
                    tokens.add(text)
            tokens.update(_ref_tokens(item))
    elif isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        for item in value:
            tokens.update(_ref_tokens(item))
    return tokens


def _relevant_rows(
    rows: Any,
    *,
    refs: set[str],
    include_pinned: bool = False,
    active_only: bool = False,
) -> list[Any]:
    if not isinstance(rows, Sequence) or isinstance(
        rows, (str, bytes, bytearray)
    ):
        return []
    selected: list[Any] = []
    deferred: list[Any] = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            deferred.append(raw)
            continue
        if active_only and _status(raw) not in _ACTIVE_STATUSES:
            continue
        pinned = bool(raw.get("locked") or raw.get("pinned"))
        row_refs = _ref_tokens(raw)
        text = _json(raw)
        relevant = bool(row_refs & refs) or any(ref in text for ref in refs if ref)
        if (include_pinned and pinned) or relevant:
            selected.append(dict(raw))
        else:
            deferred.append(dict(raw))
    return [*selected, *deferred]


@dataclass(frozen=True, slots=True)
class CompiledContext:
    sections: dict[str, Any]
    section_chars: dict[str, int]
    total_chars: int
    token_estimate: int
    fingerprint: str


class RelevantContextCompiler:
    """Deterministically select and bound the context sent to narrative models."""

    def __init__(
        self,
        *,
        section_limits: Mapping[str, int] | None = None,
        cache_enabled: bool = True,
        total_token_budget: int = DEFAULT_TOTAL_CONTEXT_TOKENS,
    ) -> None:
        self.section_limits = {
            **DEFAULT_SECTION_LIMITS,
            **{
                str(key): max(64, int(value))
                for key, value in (section_limits or {}).items()
            },
        }
        self.cache_enabled = bool(cache_enabled)
        self.total_token_budget = max(1_024, int(total_token_budget))
        self._cache: dict[str, CompiledContext] = {}

    def compile(
        self,
        *,
        world: Mapping[str, Any],
        session: Mapping[str, Any],
        sections: Mapping[str, Any],
    ) -> CompiledContext:
        world_state = (
            dict(session.get("world_state") or {})
            if isinstance(session.get("world_state"), Mapping)
            else {}
        )
        current_scene = _text(
            world_state.get("current_scene")
            or world_state.get("scene_ref")
            or world_state.get("location")
        )
        refs = _ref_tokens(
            {
                "scene": current_scene,
                "acting": sections.get("acting_player"),
                "next_actor": sections.get("next_actor"),
                "turn": sections.get("turn_context"),
                "opening": sections.get("opening_scene"),
            }
        )
        for key in ("participant_id", "character_code", "character_name"):
            for section_name in ("acting_player", "next_actor"):
                value = sections.get(section_name)
                if isinstance(value, Mapping):
                    text = _text(value.get(key))
                    if text:
                        refs.add(text)
        if current_scene:
            refs.add(current_scene)

        prepared = dict(sections)
        prepared["recent_history"] = list(
            (prepared.get("recent_history") or [])[-8:]
        )
        prepared["relevant_memories"] = _relevant_rows(
            prepared.get("relevant_memories"),
            refs=refs,
            include_pinned=True,
        )
        prepared["story_ledger"] = _relevant_rows(
            prepared.get("story_ledger"),
            refs=refs,
            active_only=True,
        )
        prepared["scene_clocks"] = _relevant_rows(
            prepared.get("scene_clocks"),
            refs=refs,
            active_only=True,
        )
        prepared["active_npcs"] = _relevant_rows(
            prepared.get("active_npcs"),
            refs=refs,
            active_only=True,
        )
        relationships = world_state.get("relationships")
        if isinstance(relationships, (Mapping, list, tuple)):
            prepared["relationship_slice"] = _relevant_rows(
                list(relationships.values())
                if isinstance(relationships, Mapping)
                else relationships,
                refs=refs,
            )
        else:
            prepared["relationship_slice"] = []

        revisions = {
            "world": _text(
                world.get("snapshot_hash")
                or world.get("content_version")
                or world.get("internal_world_model_revision")
            ),
            "session": session.get("revision"),
            "turn": session.get("turn_no"),
            "scene": current_scene,
            "actors": [
                (
                    item.get("participant_id") or item.get("id"),
                    item.get("revision"),
                )
                for item in (prepared.get("active_party") or [])
                if isinstance(item, Mapping)
            ],
            "ledger": [
                (item.get("id") or item.get("stable_key"), item.get("revision"))
                for item in (prepared.get("story_ledger") or [])
                if isinstance(item, Mapping)
            ],
        }
        source_fingerprint = hashlib.sha256(
            _json(
                {
                    "revisions": revisions,
                    "sections": prepared,
                    "limits": self.section_limits,
                    "total_token_budget": self.total_token_budget,
                }
            ).encode("utf-8")
        ).hexdigest()
        if self.cache_enabled and source_fingerprint in self._cache:
            return self._cache[source_fingerprint]

        compiled: dict[str, Any] = {}
        section_chars: dict[str, int] = {}
        for name, value in prepared.items():
            bounded = _bounded(
                value,
                self.section_limits.get(name, 4000),
            )
            compiled[name] = bounded
            section_chars[name] = len(_json(bounded))
        total_char_budget = self.total_token_budget * 3
        for name in _DEGRADE_ORDER:
            overflow = sum(section_chars.values()) - total_char_budget
            if overflow <= 0 or name not in compiled:
                break
            current = section_chars[name]
            target = max(2, current - overflow)
            compiled[name] = _bounded(compiled[name], target)
            section_chars[name] = len(_json(compiled[name]))
        if sum(section_chars.values()) > total_char_budget:
            raise ValueError(
                "必保生成上下文超过硬总预算；系统未删除当前输入、裁定或安全规则"
            )
        total_chars = sum(section_chars.values())
        fingerprint = hashlib.sha256(
            _json(compiled).encode("utf-8")
        ).hexdigest()
        result = CompiledContext(
            sections=compiled,
            section_chars=section_chars,
            total_chars=total_chars,
            token_estimate=max(1, (total_chars + 2) // 3),
            fingerprint=fingerprint,
        )
        if self.cache_enabled:
            if len(self._cache) >= 32:
                self._cache.pop(next(iter(self._cache)))
            self._cache[source_fingerprint] = result
        return result


__all__ = [
    "CompiledContext",
    "DEFAULT_TOTAL_CONTEXT_TOKENS",
    "DEFAULT_SECTION_LIMITS",
    "RelevantContextCompiler",
]
