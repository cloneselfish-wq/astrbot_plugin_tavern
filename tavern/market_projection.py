"""C5 dynamic market state, deterministic quotes and shared projection."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from .story_context import (
    build_story_condition_context,
    evaluate_story_condition,
)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("价格必须是有效十进制数") from exc


def _economy(world: Mapping[str, Any]) -> dict[str, Any]:
    rules = _mapping(world.get("rules"))
    economy = _mapping(rules.get("economy"))
    return economy


def _shops(world: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in _sequence(_economy(world).get("shops"))
        if isinstance(item, Mapping) and item.get("shop_id")
    ]


def _currency_index(world: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("id") or item.get("currency_id") or ""): dict(item)
        for item in _sequence(_economy(world).get("currencies"))
        if isinstance(item, Mapping)
        and (item.get("id") or item.get("currency_id"))
    }


def _runtime_root(runtime: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    copied = deepcopy(dict(runtime))
    nested = copied.get("runtime")
    if isinstance(nested, Mapping):
        return copied, True
    return {"runtime": copied}, False


def _condition_matches(
    condition: Any,
    *,
    world: Mapping[str, Any],
    runtime: Mapping[str, Any],
    session: Mapping[str, Any] | None = None,
    squad: Sequence[Mapping[str, Any]] = (),
) -> bool:
    context = build_story_condition_context(
        world=world,
        runtime=runtime,
        session=session,
        squad=squad,
    )
    return bool(
        evaluate_story_condition(condition, world=world, context=context).get(
            "matched"
        )
    )


def ensure_market_state(
    world: Mapping[str, Any],
    runtime: Mapping[str, Any],
    *,
    session: Mapping[str, Any] | None = None,
    squad: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Initialize missing shop runtime exactly once without resetting stock."""

    root, originally_nested = _runtime_root(runtime)
    live = _mapping(root.get("runtime"))
    economy_state = _mapping(live.get("economy"))
    shop_state = _mapping(economy_state.get("shops"))
    for shop in _shops(world):
        shop_id = str(shop.get("shop_id") or "")
        existing = _mapping(shop_state.get(shop_id))
        offers = [
            dict(item)
            for item in _sequence(shop.get("offers"))
            if isinstance(item, Mapping) and item.get("offer_id")
        ]
        stock = _mapping(existing.get("stock"))
        if not stock:
            stock = {
                str(item.get("offer_id")): max(
                    0, int(item.get("initial_stock", 0) or 0)
                )
                for item in offers
            }
        available = _condition_matches(
            shop.get("availability_conditions", {}),
            world=world,
            runtime=live,
            session=session,
            squad=squad,
        )
        current_scene = str(live.get("current_scene") or "")
        scene_refs = {
            str(item) for item in _sequence(shop.get("scene_refs")) if str(item)
        }
        if scene_refs:
            available = available and current_scene in scene_refs
        current_region = str(
            live.get("current_region")
            or live.get("region_ref")
            or ""
        )
        region_refs = {
            str(item) for item in _sequence(shop.get("region_refs")) if str(item)
        }
        if region_refs and current_region:
            available = available and current_region in region_refs
        shop_state[shop_id] = {
            "available": bool(
                existing.get("availability_override", available)
                if "availability_override" in existing
                else available
            ),
            "stock": stock,
            "price_revision": max(
                1, int(existing.get("price_revision", 1) or 1)
            ),
            "stock_revision": max(
                1, int(existing.get("stock_revision", 1) or 1)
            ),
            "price_modifiers": [
                dict(item)
                for item in _sequence(existing.get("price_modifiers"))
                if isinstance(item, Mapping)
            ],
            "last_restock_at": str(existing.get("last_restock_at") or ""),
            "blocked_reason_text_id": str(
                existing.get("blocked_reason_text_id") or ""
            ),
        }
    economy_state["shops"] = shop_state
    live["economy"] = economy_state
    root["runtime"] = live
    return root if originally_nested else live


def calculate_offer_price(
    *,
    offer: Mapping[str, Any],
    shop_state: Mapping[str, Any],
    precision: int,
) -> dict[str, Any]:
    """Calculate a bounded deterministic server quote."""

    base = _mapping(offer.get("base_price"))
    amount = _decimal(base.get("amount"))
    if amount <= 0:
        raise ValueError("商品基础价格必须大于 0")
    reasons: list[str] = []
    multiplier = Decimal("1")
    for modifier in _sequence(shop_state.get("price_modifiers")):
        if not isinstance(modifier, Mapping):
            continue
        value = _decimal(modifier.get("multiplier", 1))
        if value <= 0:
            raise ValueError("价格修正倍数必须大于 0")
        multiplier *= value
        reason = str(
            modifier.get("reason")
            or modifier.get("reason_label")
            or modifier.get("reason_text_id")
            or ""
        )
        if reason:
            reasons.append(reason)
    price = amount * multiplier
    minimum = offer.get("minimum_price")
    maximum = offer.get("maximum_price")
    if minimum is not None:
        price = max(price, _decimal(minimum))
    if maximum is not None:
        price = min(price, _decimal(maximum))
    quantum = Decimal(1).scaleb(-max(0, int(precision)))
    price = price.quantize(quantum, rounding=ROUND_HALF_UP)
    return {
        "currency_id": str(base.get("currency_id") or ""),
        "amount": format(price, f".{max(0, int(precision))}f"),
        "base_amount": format(
            amount.quantize(quantum, rounding=ROUND_HALF_UP),
            f".{max(0, int(precision))}f",
        ),
        "change_reasons": reasons,
    }


def create_market_quote(
    *,
    world: Mapping[str, Any],
    runtime: Mapping[str, Any],
    shop_ref: str,
    offer_id: str,
    quantity: int = 1,
) -> dict[str, Any]:
    """Create a stateless quote whose id commits to price and revisions."""

    quantity = int(quantity)
    if quantity <= 0:
        raise ValueError("购买数量必须大于 0")
    shops = {str(item.get("shop_id")): item for item in _shops(world)}
    shop = shops.get(str(shop_ref or ""))
    if shop is None:
        raise ValueError(f"商店未注册：{shop_ref or '（空）'}")
    offers = {
        str(item.get("offer_id")): item
        for item in _sequence(shop.get("offers"))
        if isinstance(item, Mapping) and item.get("offer_id")
    }
    offer = offers.get(str(offer_id or ""))
    if offer is None:
        raise ValueError(f"商品报价未注册：{offer_id or '（空）'}")
    initialized = ensure_market_state(world, runtime)
    live = _mapping(initialized.get("runtime")) if "runtime" in initialized else initialized
    state = _mapping(
        _mapping(_mapping(live.get("economy")).get("shops")).get(shop_ref)
    )
    if not bool(state.get("available")):
        raise ValueError("当前场景或地区未开放该商店")
    if not _condition_matches(
        offer.get("conditions", {}),
        world=world,
        runtime=live,
    ):
        raise ValueError("当前状态未解锁该商品")
    current_stock = int(_mapping(state.get("stock")).get(offer_id, 0) or 0)
    if current_stock < quantity:
        raise ValueError("商品库存不足，请重新查看集市")
    currencies = _currency_index(world)
    currency_id = str(_mapping(offer.get("base_price")).get("currency_id") or "")
    currency = currencies.get(currency_id, {})
    precision = max(0, int(currency.get("precision", 0) or 0))
    effective_state = dict(state)
    modifiers = [
        dict(item)
        for item in _sequence(state.get("price_modifiers"))
        if isinstance(item, Mapping)
    ]
    for rule in _sequence(offer.get("price_rules")):
        if not isinstance(rule, Mapping):
            continue
        if not _condition_matches(
            rule.get("when") or rule.get("conditions") or {},
            world=world,
            runtime=live,
        ):
            continue
        modifiers.append(
            {
                "id": str(rule.get("id") or ""),
                "multiplier": rule.get("multiplier", 1),
                "reason": str(
                    rule.get("reason")
                    or rule.get("reason_label")
                    or rule.get("reason_text_id")
                    or ""
                ),
            }
        )
    effective_state["price_modifiers"] = modifiers
    price = calculate_offer_price(
        offer=offer,
        shop_state=effective_state,
        precision=precision,
    )
    unit = _decimal(price["amount"])
    total = unit * quantity
    payload = {
        "shop_ref": shop_ref,
        "offer_id": offer_id,
        "quantity": quantity,
        "currency_id": price["currency_id"],
        "unit_amount": price["amount"],
        "total_amount": format(total, f".{precision}f"),
        "price_revision": int(state.get("price_revision", 1) or 1),
        "stock_revision": int(state.get("stock_revision", 1) or 1),
    }
    digest = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schema": "tavern-market-quote/1.0.0-rc10",
        "quote_id": f"quote:{digest[:32]}",
        **payload,
        "base_amount": price["base_amount"],
        "change_reasons": price["change_reasons"],
        "stock": current_stock,
    }


def project_market_view(
    *,
    world: Mapping[str, Any],
    runtime: Mapping[str, Any],
    shop_ref: str,
) -> dict[str, Any]:
    """Project one shop for BOT, prompt and WebUI consumers."""

    shops = {str(item.get("shop_id")): item for item in _shops(world)}
    shop = shops.get(str(shop_ref or ""))
    if shop is None:
        return {
            "schema": "tavern-market-view/1.0.0-rc10",
            "shop_ref": str(shop_ref or ""),
            "shop_label": "",
            "available": False,
            "blocked_reason": "当前世界没有这个商店。",
            "offers": [],
            "problems": [{"code": "unknown_shop", "message": "商店未注册"}],
        }
    initialized = ensure_market_state(world, runtime)
    live = _mapping(initialized.get("runtime")) if "runtime" in initialized else initialized
    state = _mapping(
        _mapping(_mapping(live.get("economy")).get("shops")).get(shop_ref)
    )
    currencies = _currency_index(world)
    projected: list[dict[str, Any]] = []
    problems: list[dict[str, Any]] = []
    if bool(state.get("available")):
        for offer in _sequence(shop.get("offers")):
            if not isinstance(offer, Mapping):
                continue
            offer_id = str(offer.get("offer_id") or "")
            try:
                quote = create_market_quote(
                    world=world,
                    runtime=live,
                    shop_ref=shop_ref,
                    offer_id=offer_id,
                )
            except ValueError as exc:
                problems.append(
                    {
                        "code": "offer_unavailable",
                        "offer_id": offer_id,
                        "message": str(exc),
                    }
                )
                continue
            currency = currencies.get(str(quote.get("currency_id") or ""), {})
            item_label = str(
                offer.get("item_label")
                or offer.get("name")
                or ""
            ).strip()
            currency_label = str(
                currency.get("short_name")
                or currency.get("name")
                or ""
            ).strip()
            if not item_label:
                problems.append(
                    {
                        "code": "item_label_missing",
                        "offer_id": offer_id,
                        "message": "商品名称解析失败",
                    }
                )
            if not currency_label:
                problems.append(
                    {
                        "code": "currency_label_missing",
                        "offer_id": offer_id,
                        "message": "货币名称解析失败",
                    }
                )
            projected.append(
                {
                    "offer_id": offer_id,
                    "item_ref": str(offer.get("item_ref") or ""),
                    "item_label": item_label,
                    "display_error": (
                        "商品名称解析失败，请让管理员检查世界包。"
                        if not item_label
                        else ""
                    ),
                    "description": str(offer.get("description") or ""),
                    "price": {
                        "amount": quote["unit_amount"],
                        "currency_id": quote["currency_id"],
                        "currency_label": currency_label,
                    },
                    "base_price": {
                        "amount": quote["base_amount"],
                        "currency_id": quote["currency_id"],
                        "currency_label": currency_label,
                    },
                    "stock": quote["stock"],
                    "change_reasons": quote["change_reasons"],
                    "quote_id": quote["quote_id"],
                    "price_revision": quote["price_revision"],
                    "stock_revision": quote["stock_revision"],
                }
            )
    return {
        "schema": "tavern-market-view/1.0.0-rc10",
        "shop_ref": str(shop_ref),
        "shop_label": str(shop.get("label") or shop.get("name") or ""),
        "available": bool(state.get("available")),
        "blocked_reason": str(
            state.get("blocked_reason")
            or state.get("blocked_reason_text_id")
            or ""
        ),
        "offers": projected,
        "problems": problems,
    }


__all__ = [
    "calculate_offer_price",
    "create_market_quote",
    "ensure_market_state",
    "project_market_view",
]
