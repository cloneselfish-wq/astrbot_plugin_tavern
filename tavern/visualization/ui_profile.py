"""Safe browser projection for compiled world adaptive-UI profiles."""

from __future__ import annotations

from typing import Any

from .common import integer, mapping, text


def public_ui_profile(value: Any) -> dict[str, Any]:
    """Keep declarative display data; remove release and protocol internals."""

    source = mapping(value)
    density = text(source.get("density"), limit=20, default="minimal")
    if density not in {"minimal", "standard", "rich"}:
        density = "minimal"
    pages: list[dict[str, Any]] = []
    for raw_page in source.get("pages") or ():
        page = mapping(raw_page)
        page_key = text(page.get("id"), limit=60)
        if not page_key:
            continue
        sections = []
        for raw_section in page.get("sections") or ():
            section = mapping(raw_section)
            kind = text(section.get("kind"), limit=60)
            if kind:
                sections.append(
                    {
                        "kind": kind,
                        "visibility": text(
                            section.get("visibility"),
                            limit=20,
                            default="public",
                        ),
                        "empty": text(
                            section.get("empty"),
                            limit=20,
                            default="omit",
                        ),
                    }
                )
        pages.append({"key": page_key, "sections": sections})
    party = mapping(source.get("party"))
    identity_facets = []
    for raw in party.get("identity_facets") or ():
        facet = mapping(raw)
        handle = text(facet.get("role_handle"), limit=100)
        label = text(facet.get("label"), limit=80)
        if handle and label:
            identity_facets.append(
                {
                    "role_handle": handle,
                    "label": label,
                    "priority": integer(facet.get("priority"), 100),
                    "visibility": text(
                        facet.get("visibility"),
                        limit=20,
                        default="public",
                    ),
                }
            )
    party_view: dict[str, Any] = {
        "identity_facets": identity_facets,
        "open_detail": bool(party.get("open_detail")),
    }
    for key in ("resources", "statuses", "inventory", "capabilities"):
        policy = mapping(party.get(key))
        if policy:
            party_view[key] = {
                "max_compact": max(
                    0, min(12, integer(policy.get("max_compact"), 0))
                ),
                "detail": text(policy.get("detail"), limit=40),
            }
    lenses = []
    for raw in source.get("live_lenses") or ():
        lens = mapping(raw)
        key = text(lens.get("id"), limit=60)
        label = text(lens.get("label"), limit=80)
        if key and label:
            lenses.append(
                {
                    "key": key,
                    "label": label,
                    "required": bool(lens.get("required")),
                    "order": integer(lens.get("order"), 100),
                }
            )
    statuses = []
    for raw in source.get("status_taxonomy") or ():
        status = mapping(raw)
        handle = text(status.get("role_handle"), limit=100)
        label = text(status.get("label"), limit=80)
        if handle and label:
            statuses.append(
                {
                    "role_handle": handle,
                    "label": label,
                    "tone": text(status.get("tone"), limit=30),
                    "symbol": text(status.get("symbol"), limit=30),
                    "visibility": text(
                        status.get("visibility"),
                        limit=20,
                        default="public",
                    ),
                }
            )
    visualizations = []
    for raw in source.get("visualizations") or ():
        visual = mapping(raw)
        key = text(visual.get("id"), limit=60)
        kind = text(visual.get("kind"), limit=30)
        handles = [
            text(item, limit=100)
            for item in visual.get("role_handles") or ()
            if text(item, limit=100)
        ]
        if key and kind and handles:
            scale = mapping(visual.get("scale"))
            visualizations.append(
                {
                    "key": key,
                    "kind": kind,
                    "title": text(visual.get("title"), limit=80),
                    "role_handles": handles,
                    "scale": {
                        "min": scale.get("min"),
                        "max": scale.get("max"),
                        "unit": text(scale.get("unit"), limit=24),
                    },
                    "fallback": text(
                        visual.get("fallback"), limit=30, default="list"
                    ),
                }
            )
    actor_detail = mapping(source.get("actor_detail"))
    presentation = mapping(source.get("presentation"))
    fallback = mapping(source.get("fallback_profile"))
    manifest = mapping(source.get("ui_surface_manifest"))
    surfaces = []
    for raw in manifest.get("surfaces") or ():
        surface = mapping(raw)
        surface_key = text(surface.get("surface_key"), limit=96)
        component_kind = text(surface.get("component_kind"), limit=40)
        data_kind = text(surface.get("data_kind"), limit=80)
        if surface_key and component_kind and data_kind:
            surfaces.append({
                "surface_key": surface_key,
                "component_kind": component_kind,
                "data_kind": data_kind,
                "placements": [text(item, limit=40) for item in surface.get("placements") or () if text(item, limit=40)],
                "label": text(surface.get("label"), limit=100),
                "summary": text(surface.get("summary"), limit=300),
                "usage": text(surface.get("usage"), limit=20),
                "audience_scopes": [text(item, limit=20) for item in surface.get("audience_scopes") or () if text(item, limit=20)],
                "empty_policy": text(surface.get("empty_policy"), limit=40),
                "mobile_presentation": text(surface.get("mobile_presentation"), limit=64),
                "visual_recipe": text(surface.get("visual_recipe"), limit=80),
                "refresh": {
                    "mode": text(mapping(surface.get("refresh")).get("mode"), limit=20),
                    "event_types": [
                        text(item, limit=80)
                        for item in mapping(surface.get("refresh")).get("event_types") or ()
                        if text(item, limit=80)
                    ],
                },
                "copy": {
                    key: text(mapping(surface.get("copy")).get(key), limit=300)
                    for key in (
                        "title", "summary", "help", "impact", "boundary",
                        "empty", "error_operation", "error_reason",
                        "automatic_action", "recovery",
                    )
                },
                "required": bool(surface.get("required")),
                "order": integer(surface.get("order"), 0),
            })
    return {
        "density": density,
        "empty_policy": "omit-unsupported",
        "pages": pages,
        "party": party_view,
        "actor_detail": {
            "sections": [
                text(item, limit=40)
                for item in actor_detail.get("sections") or ()
                if text(item, limit=40)
            ],
            "default_section": text(
                actor_detail.get("default_section"),
                limit=40,
                default="identity",
            ),
        },
        "live_lenses": sorted(
            lenses, key=lambda item: (item["order"], item["key"])
        ),
        "status_taxonomy": statuses,
        "visualizations": visualizations,
        "presentation": {
            "style": text(presentation.get("style"), limit=30),
            "preferred_block_kinds": [
                text(item, limit=30)
                for item in presentation.get("preferred_block_kinds") or ()
                if text(item, limit=30)
            ],
            "dialogue_density": text(
                presentation.get("dialogue_density"), limit=20
            ),
            "scene_break_label": text(
                presentation.get("scene_break_label"), limit=20
            ),
            "allow_title": bool(presentation.get("allow_title", True)),
        },
        "fallback": {
            "used": bool(fallback.get("used")),
            "attribute_fallback": text(
                fallback.get("attribute_fallback"),
                limit=20,
                default="list",
            ),
        },
        "ui_surface_manifest": {
            "world_revision": text(manifest.get("world_revision"), limit=100),
            "manifest_revision": text(manifest.get("manifest_revision"), limit=100),
            "profile_revision": text(manifest.get("profile_revision"), limit=100),
            "component_registry_version": text(manifest.get("component_registry_version"), limit=40),
            "surfaces": sorted(surfaces, key=lambda item: (item["order"], item["surface_key"])),
        },
    }


__all__ = ["public_ui_profile"]
