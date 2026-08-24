from .common import *
from .system import *
from .context import *

def _ledger_projection(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    keys = ("id", "stable_key", "kind", "title", "description", "status", "visibility")
    return [{key: item.get(key) for key in keys if key in item} for item in items]


def _inventory_projection(
    instances: Sequence[Mapping[str, Any]],
    player: Mapping[str, Any],
) -> dict[str, Any]:
    """Project the authoritative item_instances supplied by the engine."""

    items = []
    for instance in instances:
        if not isinstance(instance, Mapping):
            continue
        quantity = max(0, int(instance.get("quantity", 0) or 0))
        if quantity <= 0:
            continue
        items.append(
            {
                "item_ref": str(instance.get("item_id") or ""),
                "label": str(
                    instance.get("item_label")
                    or instance.get("label")
                    or ""
                ),
                "quantity": quantity,
                "quality": str(instance.get("quality") or "standard"),
                "container": str(instance.get("container") or ""),
            }
        )
    return {
        "owner_ref": str(
            player.get("participant_id")
            or player.get("id")
            or ""
        ),
        "items": items,
    }


def _shop_projection(
    world: Mapping[str, Any],
    world_state: Mapping[str, Any],
) -> dict[str, Any]:
    """Return current MarketView data; never static definition stock."""

    contract = world_contract(world)
    economy = contract.get("economy") or {}
    shops = [
        item
        for item in (economy.get("shops") or [])
        if isinstance(item, Mapping)
    ]
    if not shops:
        return {"available": False, "offers": [], "problems": []}
    from ..protocol.runtime import flatten_runtime, runtime_from_state

    runtime = flatten_runtime(runtime_from_state(world_state))
    views = [
        project_market_view(
            world=world,
            runtime=runtime,
            shop_ref=str(shop.get("shop_id") or ""),
        )
        for shop in shops
    ]
    return next(
        (item for item in views if item.get("available")),
        views[0],
    )


def _runtime_sections(
    *,
    world: Mapping[str, Any],
    session: Mapping[str, Any],
    player: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    memories: Sequence[Mapping[str, Any]],
) -> str:
    acting = _character_projection(player, world)
    next_actor = _character_projection(
        session.get("next_actor", {})
        if isinstance(session.get("next_actor"), Mapping)
        else {},
        world,
    )
    if (
        acting.get("participant_id")
        and acting.get("participant_id") == next_actor.get("participant_id")
    ):
        next_actor = {
            "participant_id": acting["participant_id"],
            "same_as_acting_player": True,
        }
    world_state = dict(session.get("world_state", {})) if isinstance(session.get("world_state"), Mapping) else {}
    world_state.pop("runtime", None)
    twp_projection = twp_runtime_projection(
        world, session.get("world_state", {})
    )
    opening_projection = session.get("opening_scene_projection")
    sections = {
        "opening_scene": (
            opening_projection
            if isinstance(opening_projection, Mapping)
            and opening_projection.get("declared")
            else {}
        ),
        "runtime_state": world_state,
        "world_module_runtime": twp_projection,
        "turn_context": session.get("turn_status", {}),
        "active_party": _party(session.get("roster", [])),
        "acting_player": acting,
        "acting_inventory": _inventory_projection(
            session.get("item_instances", []), player
        ),
        "shop": _shop_projection(world, session.get("world_state", {})),
        "entity_candidates": item_candidate_projection(
            world,
            session,
            player,
            limit=int(
                (
                    session.get("context_budget", {}).get(
                        "entity_candidates",
                        32,
                    )
                    if isinstance(session.get("context_budget"), Mapping)
                    else 32
                )
                or 32
            ),
        ),
        "next_actor": next_actor,
        "relevant_memories": _memory_projection(memories),
        "recent_history": _history(events),
        "active_return_requests": session.get("return_requests", []),
        "active_npcs": _npc_projection(
            session.get("session_characters", []), world
        ),
        "story_ledger": _ledger_projection(
            session.get("story_ledger", [])
        ),
        "scene_clocks": session.get("scene_clocks", []),
        "content_boundaries": session.get("content_boundaries", {}),
    }
    budget = (
        session.get("context_budget")
        if isinstance(session.get("context_budget"), Mapping)
        else {}
    )
    compiler = _context_compiler(budget)
    compiled = compiler.compile(
        world=world,
        session=session,
        sections=sections,
    )
    trust = {
        "opening_scene": "plugin-authoritative",
        "world_module_runtime": "plugin-authoritative",
        "next_actor": "plugin-authoritative",
        "content_boundaries": "trusted-policy",
    }
    blocks = []
    for name, value in compiled.sections.items():
        if name == "opening_scene" and not value:
            continue
        blocks.append(
            f'<{name} trust="{trust.get(name, "untrusted-data")}">\n'
            f"{_json(value)}\n"
            f"</{name}>"
        )
    return "\n\n".join(blocks) + "\n"


__all__ = [name for name in globals() if not name.startswith('__')]

