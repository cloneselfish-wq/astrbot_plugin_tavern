from __future__ import annotations

from .economy_support import *


class WalletsRepositoryMixin:
    async def economy_state(self, session_id: str) -> dict[str, Any]:
        return await self._run(self._economy_state, session_id)

    def _economy_state(self, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT enabled FROM economy_state WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return {"session_id": session_id, "enabled": bool(row and row["enabled"])}

    def _currency_precision(
        self,
        connection: Any,
        session_id: str,
        currency_id: str,
    ) -> int:
        row = connection.execute(
            """
            SELECT precision FROM economy_currencies
            WHERE session_id = ? AND currency_id = ?
            """,
            (session_id, currency_id),
        ).fetchone()
        return int(row["precision"]) if row else 0

    async def economy_summary(self, session_id: str) -> dict[str, Any]:
        result = await self._run(self._economy_summary, session_id)
        result["capability"] = await self.economy_capability(session_id)
        return result

    def _economy_summary(self, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            state = self._economy_state(session_id)
            currencies = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT currency_id, name, short_name, icon, precision,
                           allow_negative, transferable, exchangeable, public,
                           sort_order, extensions_json
                    FROM economy_currencies WHERE session_id = ?
                    ORDER BY sort_order, currency_id
                    """,
                    (session_id,),
                ).fetchall()
            ]
            wallets = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT owner_type, owner_ref, currency_id, balance
                    FROM economy_wallets WHERE session_id = ?
                    ORDER BY owner_type, owner_ref, currency_id
                    """,
                    (session_id,),
                ).fetchall()
            ]
            recent = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT operation_id, kind, currency_id, from_owner_type,
                           from_owner_ref, to_owner_type, to_owner_ref, amount,
                           balance_before, balance_after, reason, source,
                           actor_id, status, created_at
                    FROM economy_transactions WHERE session_id = ?
                    ORDER BY created_at DESC, rowid DESC LIMIT 20
                    """,
                    (session_id,),
                ).fetchall()
            ]
            rules = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT from_currency, to_currency, rate_numerator,
                           rate_denominator, fee, enabled
                    FROM economy_exchange_rules WHERE session_id = ?
                    ORDER BY from_currency, to_currency
                    """,
                    (session_id,),
                ).fetchall()
            ]
        currency_by_id = {
            str(item["currency_id"]): item for item in currencies
        }
        currency_views = [
            {
                **item,
                "label": str(item.get("name") or item["currency_id"]),
                "short_label": str(
                    item.get("short_name")
                    or item.get("name")
                    or item["currency_id"]
                ),
            }
            for item in currencies
        ]
        wallet_views = []
        for wallet in wallets:
            meta = currency_by_id.get(str(wallet["currency_id"]), {})
            view = currency_view(
                str(wallet["currency_id"]),
                int(wallet["balance"]),
                precision=int(meta.get("precision") or 0),
                label=str(meta.get("name") or ""),
                short_label=str(meta.get("short_name") or ""),
                icon=str(meta.get("icon") or ""),
            ).to_dict()
            wallet_views.append({**wallet, **view})
        recent_views = []
        for item in recent:
            meta = currency_by_id.get(str(item["currency_id"]), {})
            recent_views.append(
                {
                    **item,
                    "currency_label": str(
                        meta.get("name") or item["currency_id"]
                    ),
                    "formatted_amount": format_money(
                        str(item["currency_id"]),
                        int(item["amount"]),
                        precision=int(meta.get("precision") or 0),
                        label=str(meta.get("name") or ""),
                        short_label=str(meta.get("short_name") or ""),
                        icon=str(meta.get("icon") or ""),
                    ),
                }
            )
        return {
            "enabled": state["enabled"],
            "currencies": currency_views,
            "wallets": wallet_views,
            "recent": recent_views,
            "exchange_rules": rules,
        }

    async def economy_balance(
        self,
        session_id: str,
        owner_type: str,
        owner_ref: str,
        currency_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._economy_balance,
            session_id,
            owner_type,
            owner_ref,
            currency_id,
        )

    def _economy_balance(
        self,
        session_id: str,
        owner_type: str,
        owner_ref: str,
        currency_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            self._require_enabled(connection, session_id)
            row = connection.execute(
                """
                SELECT balance FROM economy_wallets
                WHERE session_id = ? AND owner_type = ? AND owner_ref = ?
                  AND currency_id = ?
                """,
                (session_id, owner_type, owner_ref, currency_id),
            ).fetchone()
            currency = connection.execute(
                """
                SELECT name, short_name, icon, precision
                FROM economy_currencies
                WHERE session_id = ? AND currency_id = ?
                """,
                (session_id, currency_id),
            ).fetchone()
            precision = int(currency["precision"]) if currency else 0
            view = currency_view(
                currency_id,
                int(row["balance"]) if row else 0,
                precision=precision,
                label=str(currency["name"]) if currency else "",
                short_label=str(currency["short_name"]) if currency else "",
                icon=str(currency["icon"]) if currency else "",
            )
        return {
            "ok": True,
            "session_id": session_id,
            "owner_type": owner_type,
            "owner_ref": owner_ref,
            "currency_id": currency_id,
            "balance": int(row["balance"]) if row else 0,
            "balance_major": _minor_to_major(
                int(row["balance"]) if row else 0, precision
            ),
            **view.to_dict(),
        }

    # ── 核心事务操作 ──────────────────────────────────────────────