"""C5 atomic purchase from a server-authoritative market quote."""
from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from ..database_support import *
from ..market_projection import create_market_quote, ensure_market_state
from ..world_contract import world_contract


def _major_to_minor(value: Any, precision: int) -> int:
    precision = max(0, int(precision))
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("金额格式无效") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError("金额必须大于 0")
    scaled = amount * (Decimal(10) ** precision)
    integral = scaled.to_integral_value()
    if scaled != integral:
        raise ValueError(f"金额小数位超过货币精度 {precision}")
    return int(integral)


def _minor_to_major(value: int, precision: int) -> str:
    precision = max(0, int(precision))
    integer = int(value)
    if precision == 0:
        return str(integer)
    sign = "-" if integer < 0 else ""
    digits = str(abs(integer)).zfill(precision + 1)
    return f"{sign}{digits[:-precision]}.{digits[-precision:]}"


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


class AtomicPurchaseMixin:
    async def atomic_purchase(
        self,
        *,
        session_id: str,
        operation_id: str,
        owner: str,
        owner_type: str,
        shop_ref: str,
        offer_id: str,
        quantity: int,
        quote_id: str,
        expected_price_revision: int,
        expected_stock_revision: int,
        actor_id: str,
        reason: str = "购买商品",
    ) -> dict[str, Any]:
        return await self._run(
            self._atomic_purchase,
            session_id,
            operation_id,
            owner,
            owner_type,
            shop_ref,
            offer_id,
            quantity,
            quote_id,
            expected_price_revision,
            expected_stock_revision,
            actor_id,
            reason,
        )

    def _atomic_purchase(
        self,
        session_id: str,
        operation_id: str,
        owner: str,
        owner_type: str,
        shop_ref: str,
        offer_id: str,
        quantity: int,
        quote_id: str,
        expected_price_revision: int,
        expected_stock_revision: int,
        actor_id: str,
        reason: str,
    ) -> dict[str, Any]:
        quantity = int(quantity)
        if quantity <= 0:
            raise ValueError("购买数量必须大于 0")
        request = {
            "purchase": True,
            "owner": owner,
            "owner_type": owner_type,
            "shop_ref": shop_ref,
            "offer_id": offer_id,
            "quantity": quantity,
            "quote_id": quote_id,
            "price_revision": int(expected_price_revision),
            "stock_revision": int(expected_stock_revision),
        }
        input_hash = self._items_input_hash(request)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._require_running_asset_action(connection, session_id)
                self._require_enabled(connection, session_id)
                existing = connection.execute(
                    """
                    SELECT input_hash, result_json FROM operation_commits
                    WHERE operation_id = ? AND session_id = ?
                    """,
                    (operation_id, session_id),
                ).fetchone()
                if existing:
                    if str(existing["input_hash"] or "") != input_hash:
                        raise DatabaseConflictError(
                            "幂等操作 ID 已用于另一笔购买"
                        )
                    payload = json_load(existing["result_json"], {})
                    payload["replayed"] = True
                    connection.execute("COMMIT")
                    return payload

                session = connection.execute(
                    """
                    SELECT world_id, world_state_json, revision
                    FROM sessions WHERE id = ?
                    """,
                    (session_id,),
                ).fetchone()
                if not session:
                    raise DatabaseNotFoundError("副本不存在")
                config = connection.execute(
                    """
                    SELECT world_snapshot_json
                    FROM instance_configs WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                world = (
                    json_load(config["world_snapshot_json"], {})
                    if config
                    else self._world_snapshot_for(
                        connection, str(session["world_id"] or "")
                    )
                )
                if not isinstance(world, Mapping):
                    raise ValueError("当前副本缺少冻结世界快照")
                state = json_load(session["world_state_json"], {})
                state = dict(state) if isinstance(state, Mapping) else {}
                from ..protocol.runtime import (
                    flatten_runtime,
                    hydrate_runtime,
                    runtime_from_state,
                    store_runtime,
                )

                runtime_root = runtime_from_state(state)
                runtime = flatten_runtime(runtime_root)
                runtime = ensure_market_state(world, runtime)
                try:
                    fresh_quote = create_market_quote(
                        world=world,
                        runtime=runtime,
                        shop_ref=shop_ref,
                        offer_id=offer_id,
                        quantity=quantity,
                    )
                except ValueError:
                    connection.execute("ROLLBACK")
                    return {
                        "ok": False,
                        "reason": "quote_expired",
                        "message": "商品价格或库存已经变化，请重新查看集市后再购买。",
                        "operation_id": operation_id,
                    }
                if (
                    str(fresh_quote.get("quote_id") or "") != str(quote_id or "")
                    or int(fresh_quote.get("price_revision", 0) or 0)
                    != int(expected_price_revision)
                    or int(fresh_quote.get("stock_revision", 0) or 0)
                    != int(expected_stock_revision)
                ):
                    connection.execute("ROLLBACK")
                    return {
                        "ok": False,
                        "reason": "quote_expired",
                        "message": "商品价格或库存已经变化，请重新查看集市后再购买。",
                        "operation_id": operation_id,
                    }

                contract = world_contract(world)
                shops = {
                    str(item.get("shop_id") or ""): item
                    for item in contract.get("economy", {}).get("shops", [])
                    if isinstance(item, Mapping)
                }
                shop = _mapping(shops.get(shop_ref))
                offers = {
                    str(item.get("offer_id") or ""): item
                    for item in shop.get("offers", [])
                    if isinstance(item, Mapping)
                }
                offer = _mapping(offers.get(offer_id))
                if not offer:
                    raise ValueError("商品报价已经从当前世界移除")
                item_id = str(offer.get("item_ref") or "")
                item_label = str(
                    offer.get("item_label")
                    or offer.get("name")
                    or item_id
                )
                currency_id = str(fresh_quote.get("currency_id") or "")
                precision = self._currency_precision(
                    connection, session_id, currency_id
                )
                total_minor = _major_to_minor(
                    fresh_quote.get("total_amount"), precision
                )
                currency = connection.execute(
                    """
                    SELECT allow_negative FROM economy_currencies
                    WHERE session_id = ? AND currency_id = ?
                    """,
                    (session_id, currency_id),
                ).fetchone()
                if not currency:
                    raise ValueError(f"货币未定义：{currency_id}")
                self._ensure_wallet(
                    connection, session_id, owner_type, owner, currency_id
                )
                balance_row = connection.execute(
                    """
                    SELECT balance FROM economy_wallets
                    WHERE session_id = ? AND owner_type = ? AND owner_ref = ?
                      AND currency_id = ?
                    """,
                    (session_id, owner_type, owner, currency_id),
                ).fetchone()
                balance = int(balance_row["balance"]) if balance_row else 0
                if balance < total_minor and not currency["allow_negative"]:
                    connection.execute("ROLLBACK")
                    return {
                        "ok": False,
                        "reason": "insufficient_funds",
                        "message": "余额不足，系统未扣款也未发放商品。请更换商品或补充资金后重试。",
                        "operation_id": operation_id,
                    }

                economy_state = _mapping(runtime.get("economy"))
                shop_states = _mapping(economy_state.get("shops"))
                shop_state = _mapping(shop_states.get(shop_ref))
                stock = _mapping(shop_state.get("stock"))
                current_stock = int(stock.get(offer_id, 0) or 0)
                if current_stock < quantity:
                    connection.execute("ROLLBACK")
                    return {
                        "ok": False,
                        "reason": "out_of_stock",
                        "message": f"『{item_label}』库存不足，系统未扣款。请重新查看集市。",
                        "operation_id": operation_id,
                    }
                remaining = current_stock - quantity
                stock[offer_id] = remaining
                shop_state["stock"] = stock
                shop_state["stock_revision"] = (
                    int(shop_state.get("stock_revision", 1) or 1) + 1
                )
                shop_states[shop_ref] = shop_state
                economy_state["shops"] = shop_states
                runtime["economy"] = economy_state

                self._ensure_wallet(
                    connection, session_id, "shop", shop_ref, currency_id
                )
                shop_row = connection.execute(
                    """
                    SELECT balance FROM economy_wallets
                    WHERE session_id = ? AND owner_type = 'shop'
                      AND owner_ref = ? AND currency_id = ?
                    """,
                    (session_id, shop_ref, currency_id),
                ).fetchone()
                shop_balance = int(shop_row["balance"]) if shop_row else 0
                from_after = balance - total_minor
                to_after = shop_balance + total_minor
                now = utc_now()
                connection.execute(
                    """
                    UPDATE economy_wallets SET balance = ?, updated_at = ?
                    WHERE session_id = ? AND owner_type = ? AND owner_ref = ?
                      AND currency_id = ?
                    """,
                    (
                        from_after,
                        now,
                        session_id,
                        owner_type,
                        owner,
                        currency_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE economy_wallets SET balance = ?, updated_at = ?
                    WHERE session_id = ? AND owner_type = 'shop'
                      AND owner_ref = ? AND currency_id = ?
                    """,
                    (to_after, now, session_id, shop_ref, currency_id),
                )
                tx_id = new_id("etx")
                connection.execute(
                    """
                    INSERT INTO economy_transactions(
                        id, session_id, operation_id, kind, currency_id,
                        from_owner_type, from_owner_ref, to_owner_type,
                        to_owner_ref, amount, balance_before, balance_after,
                        reason, source, actor_id, target_ref, event_id,
                        status, created_at
                    ) VALUES (?, ?, ?, 'purchase', ?, ?, ?, 'shop', ?, ?,
                              ?, ?, ?, 'market_quote', ?, ?, '',
                              'committed', ?)
                    """,
                    (
                        tx_id,
                        session_id,
                        operation_id,
                        currency_id,
                        owner_type,
                        owner,
                        shop_ref,
                        total_minor,
                        balance,
                        from_after,
                        reason,
                        actor_id,
                        offer_id,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO item_instances(
                        id, session_id, owner_type, owner_ref, item_id,
                        quantity, quality, durability, charges, binding,
                        container, source, state_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'standard', 0, 0, 'none',
                              '', ?, ?, ?, ?)
                    ON CONFLICT(session_id, owner_ref, item_id, container)
                    DO UPDATE SET quantity = quantity + excluded.quantity,
                                  updated_at = excluded.updated_at
                    """,
                    (
                        new_id("item_instance"),
                        session_id,
                        owner_type,
                        owner,
                        item_id,
                        quantity,
                        f"purchase:{shop_ref}:{offer_id}",
                        json_dump(
                            {
                                "quote_id": quote_id,
                                "shop_ref": shop_ref,
                                "offer_id": offer_id,
                            }
                        ),
                        now,
                        now,
                    ),
                )
                runtime["revision"] = int(runtime.get("revision", 0) or 0) + 1
                store_runtime(
                    state,
                    hydrate_runtime(
                        runtime,
                        artifact_id=str(
                            runtime_root.get("artifact_id")
                            or world.get("artifact_id")
                            or ""
                        ),
                        enabled_modules=list(
                            runtime_root.get("enabled_modules") or []
                        ),
                    ),
                )
                connection.execute(
                    """
                    UPDATE sessions SET world_state_json = ?,
                        revision = revision + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (json_dump(state), now, session_id),
                )
                result = {
                    "ok": True,
                    "operation_id": operation_id,
                    "shop_ref": shop_ref,
                    "offer_id": offer_id,
                    "quote_id": quote_id,
                    "item_id": item_id,
                    "item_label": item_label,
                    "quantity": quantity,
                    "currency_id": currency_id,
                    "amount": total_minor,
                    "amount_major": _minor_to_major(total_minor, precision),
                    "remaining_stock": remaining,
                    "price_revision": int(expected_price_revision),
                    "stock_revision": int(shop_state["stock_revision"]),
                    "replayed": False,
                }
                connection.execute(
                    """
                    INSERT INTO operation_commits(
                        operation_id, session_id, input_hash, status,
                        result_json, rollback_json, created_at, updated_at
                    ) VALUES (?, ?, ?, 'completed', ?, '{}', ?, ?)
                    """,
                    (
                        operation_id,
                        session_id,
                        input_hash,
                        json_dump(result),
                        now,
                        now,
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "economy.purchase",
                    operation_id,
                    {
                        "owner": owner,
                        "shop_ref": shop_ref,
                        "offer_id": offer_id,
                        "item": item_id,
                        "quantity": quantity,
                        "currency_id": currency_id,
                        "amount": total_minor,
                        "remaining_stock": remaining,
                        "price_revision": int(expected_price_revision),
                        "stock_revision": int(shop_state["stock_revision"]),
                    },
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise


__all__ = [
    "AtomicPurchaseMixin",
    "_major_to_minor",
    "_minor_to_major",
]
