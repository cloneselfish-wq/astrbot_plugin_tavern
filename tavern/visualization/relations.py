"""Permission-cropped RelationGraph projection."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .common import display_label, mapping, number_or_none, text, visible
from .keys import OpaqueKeyFactory


def _entity_labels(
    world: Mapping[str, Any],
    roster: Sequence[Mapping[str, Any]] | None,
    npcs: Sequence[Mapping[str, Any]] | None,
) -> tuple[dict[str, str], dict[str, str]]:
    labels: dict[str, str] = {}
    kinds: dict[str, str] = {}
    for raw in world.get("entity_index") or ():
        if not isinstance(raw, Mapping):
            continue
        label = display_label(raw.get("label") or raw.get("name"))
        kind = text(raw.get("type"), limit=30, default="entity")
        if not label:
            continue
        for candidate in (
            raw.get("id"),
            raw.get("short_ref"),
            raw.get("canonical_ref"),
        ):
            ref = text(candidate, limit=180)
            if ref:
                labels[ref] = label
                kinds[ref] = kind
    for raw in roster or ():
        if not isinstance(raw, Mapping):
            continue
        label = display_label(
            raw.get("character_name") or raw.get("display_name")
        )
        if not label:
            continue
        for candidate in (
            raw.get("id"),
            raw.get("group_user_id"),
            raw.get("user_id"),
            raw.get("character_name"),
        ):
            ref = text(candidate, limit=180)
            if ref:
                labels[ref] = label
                kinds[ref] = "character"
    for raw in npcs or ():
        if not isinstance(raw, Mapping):
            continue
        label = display_label(raw.get("name"))
        if not label:
            continue
        for candidate in (raw.get("id"), raw.get("stable_key"), raw.get("name")):
            ref = text(candidate, limit=180)
            if ref:
                labels[ref] = label
                kinds[ref] = "npc"
    return labels, kinds


def _relation_rows(value: Any) -> list[tuple[str, str, dict[str, Any]]]:
    rows: list[tuple[str, str, dict[str, Any]]] = []
    if isinstance(value, Mapping):
        for raw_key, raw_value in value.items():
            key = text(raw_key, limit=380)
            if "→" not in key:
                continue
            source, target = (part.strip() for part in key.split("→", 1))
            if not source or not target:
                continue
            detail = mapping(raw_value)
            if not detail and isinstance(raw_value, (int, float, str)):
                detail = {"value": raw_value}
            rows.append((source, target, detail))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for raw in value:
            if not isinstance(raw, Mapping):
                continue
            source = text(raw.get("source"), limit=180)
            target = text(raw.get("target"), limit=180)
            if source and target:
                rows.append((source, target, dict(raw)))
    return rows


def project_relations(
    relationships: Any,
    *,
    world: Mapping[str, Any],
    roster: Sequence[Mapping[str, Any]] | None,
    npcs: Sequence[Mapping[str, Any]] | None,
    viewer_refs: set[str] | None,
    privileged: bool,
    keys: OpaqueKeyFactory,
    node_limit: int = 20,
    edge_limit: int = 30,
) -> dict[str, Any]:
    labels, kinds = _entity_labels(world, roster, npcs)
    viewer_refs = {str(value) for value in viewer_refs or () if str(value)}
    visible_edges: list[tuple[str, str, dict[str, Any]]] = []
    for source, target, detail in _relation_rows(relationships):
        self_owned = source in viewer_refs or target in viewer_refs
        # Historical relationship rows did not carry visibility.  Fail closed
        # for ordinary players unless the relationship belongs to that player.
        raw_visibility = detail.get("visibility")
        if raw_visibility in (None, "") and not privileged and not self_owned:
            continue
        if not visible(
            raw_visibility or ("self" if self_owned else "private"),
            privileged=privileged,
            self_owned=self_owned,
        ):
            continue
        if source not in labels or target not in labels:
            continue
        visible_edges.append((source, target, detail))

    safe_refs: list[str] = []
    for source, target, _detail in visible_edges:
        if source not in safe_refs:
            safe_refs.append(source)
        if target not in safe_refs:
            safe_refs.append(target)
    safe_total_nodes = len(safe_refs)
    node_limit = max(2, min(50, int(node_limit)))
    selected_refs = safe_refs[:node_limit]
    selected = set(selected_refs)
    nodes = [
        {
            "key": keys.key("relationnode", ref),
            "label": labels[ref],
            "kind": kinds.get(ref, "entity"),
            "state": "active",
        }
        for ref in selected_refs
    ]
    key_by_ref = {
        ref: keys.key("relationnode", ref) for ref in selected_refs
    }
    edge_limit = max(1, min(100, int(edge_limit)))
    edges: list[dict[str, Any]] = []
    for source, target, detail in visible_edges:
        if source not in selected or target not in selected:
            continue
        direction = text(detail.get("direction"), limit=20, default="forward")
        if direction not in {"forward", "backward", "both", "none"}:
            direction = "forward"
        strength = number_or_none(
            detail.get("strength", detail.get("value"))
        )
        if strength is None:
            strength = next(
                (
                    number_or_none(value)
                    for key, value in detail.items()
                    if key not in {"visibility", "direction"}
                    and number_or_none(value) is not None
                ),
                None,
            )
        relation_kind = text(detail.get("kind"), limit=30, default="relationship")
        if relation_kind not in {
            "trust",
            "favor",
            "hostile",
            "allied",
            "family",
            "relationship",
        }:
            relation_kind = "relationship"
        edges.append(
            {
                "source": key_by_ref[source],
                "target": key_by_ref[target],
                "kind": relation_kind,
                "label": text(
                    detail.get("label") or detail.get("summary"),
                    limit=80,
                    default="关系已记录",
                ),
                "direction": direction,
                "strength": strength,
            }
        )
        if len(edges) >= edge_limit:
            break
    focus_ref = next(
        (ref for ref in selected_refs if ref in viewer_refs),
        selected_refs[0] if selected_refs else "",
    )
    return {
        "focus_key": key_by_ref.get(focus_ref, ""),
        "nodes": nodes,
        "edges": edges,
        "truncated": (
            safe_total_nodes > len(nodes) or len(visible_edges) > len(edges)
        ),
        "total_nodes": safe_total_nodes,
        "continuation": {
            "can_search": safe_total_nodes > len(nodes),
            "mode": "focus_neighborhood",
        },
        "problems": [],
    }


__all__ = ["project_relations"]
