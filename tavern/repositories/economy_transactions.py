from __future__ import annotations

from .economy_support import *


class EconomyTransactionsRepositoryMixin:
    async def economy_apply(
        self,
        *,
        session_id: str,
        operation_id: str,
        kind: str,
        currency_id: str,
        amount: Any,
        from_owner: tuple[str, str] | None = None,
        to_owner: tuple[str, str] | None = None,
        reason: str = "",
        source: str = ECONOMY_SOURCE_WEB,
        actor_id: str = "",
        target_ref: str = "",
        event_id: str = "",
    ) -> dict[str, Any]:
        return await self._run(
            self._economy_apply,
            session_id,
            str(operation_id or "").strip(),
            str(kind or "adjust").strip(),
            str(currency_id or "").strip(),
            amount,
            from_owner,
            to_owner,
            str(reason or "").strip(),
            str(source or "").strip(),
            str(actor_id or "").strip(),
            str(target_ref or "").strip(),
            str(event_id or "").strip(),
        )

    def _economy_apply(
        self,
        session_id: str,
        operation_id: str,
        kind: str,
        currency_id: str,
        amount: Any,
        from_owner: tuple[str, str] | None,
        to_owner: tuple[str, str] | None,
        reason: str,
        source: str,
        actor_id: str,
        target_ref: str,
        event_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                return self._economy_apply_locked(
                    connection,
                    session_id=session_id,
                    operation_id=operation_id,
                    kind=kind,
                    currency_id=currency_id,
                    amount=amount,
                    from_owner=from_owner,
                    to_owner=to_owner,
                    reason=reason,
                    source=source,
                    actor_id=actor_id,
                    target_ref=target_ref,
                    event_id=event_id,
                )
            except InsufficientFundsError:
                connection.execute("ROLLBACK")
                return {
                    "ok": False,
                    "operation_id": operation_id,
                    "session_id": session_id,
                    "kind": kind,
                    "currency_id": currency_id,
                    "reason": "insufficient_funds",
                    "message": f"{currency_id} 余额不足",
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _economy_apply_locked(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        operation_id: str,
        kind: str,
        currency_id: str,
        amount: Any,
        from_owner: tuple[str, str] | None = None,
        to_owner: tuple[str, str] | None = None,
        reason: str = "",
        source: str = ECONOMY_SOURCE_WEB,
        actor_id: str = "",
        target_ref: str = "",
        event_id: str = "",
    ) -> dict[str, Any]:
        """在调用方事务内应用一笔经济操作（与回合提交共用同一事务）。

        失败（余额不足等）直接抛错，由调用方事务整体回滚；
        幂等检查与 C5 行为一致（operation_id 全局唯一，重复返回首次结果）。
        """
        if not operation_id:
            raise ValueError("经济操作必须提供 operation_id（幂等键）")
        if not currency_id:
            raise ValueError("缺少货币 currency_id")
        if not from_owner and not to_owner:
            raise ValueError("经济操作至少需要一个钱包方向")
        self._assert_session_writable(connection, session_id)
        self._require_enabled(connection, session_id)
        existing = connection.execute(
            """
            SELECT * FROM economy_transactions
            WHERE session_id = ? AND operation_id = ?
            """,
            (session_id, operation_id),
        ).fetchone()
        if existing:
            return self._tx_result(existing)
        precision = self._currency_precision(
            connection, session_id, currency_id
        )
        minor = _major_to_minor(amount, precision)
        if minor == 0:
            raise ValueError("经济操作金额不能为 0")
        currency = connection.execute(
            """
            SELECT allow_negative, transferable, name, short_name,
                   icon, precision
            FROM economy_currencies
            WHERE session_id = ? AND currency_id = ?
            """,
            (session_id, currency_id),
        ).fetchone()
        if not currency:
            raise ValueError(f"货币未定义：{currency_id}")

        def wallet_balance(owner: tuple[str, str] | None) -> int:
            if not owner:
                return 0
            row = connection.execute(
                """
                SELECT balance FROM economy_wallets
                WHERE session_id = ? AND owner_type = ? AND owner_ref = ?
                  AND currency_id = ?
                """,
                (session_id, owner[0], owner[1], currency_id),
            ).fetchone()
            return int(row["balance"]) if row else 0

        from_before = from_after = 0
        if from_owner:
            self._ensure_wallet(
                connection, session_id, from_owner[0], from_owner[1],
                currency_id,
            )
            from_before = wallet_balance(from_owner)
            from_after = from_before - minor
            if from_after < 0 and not currency["allow_negative"]:
                required_text = format_money(
                    currency_id,
                    minor,
                    precision=precision,
                    label=str(currency["name"]),
                    short_label=str(currency["short_name"]),
                    icon=str(currency["icon"]),
                )
                available_text = format_money(
                    currency_id,
                    from_before,
                    precision=precision,
                    label=str(currency["name"]),
                    short_label=str(currency["short_name"]),
                    icon=str(currency["icon"]),
                )
                raise InsufficientFundsError(
                    f"{currency['name']}不足：需要 {required_text}，"
                    f"当前有 {available_text}"
                )
            connection.execute(
                """
                UPDATE economy_wallets SET balance = ?, updated_at = ?
                WHERE session_id = ? AND owner_type = ? AND owner_ref = ?
                  AND currency_id = ?
                """,
                (
                    from_after, utc_now(), session_id,
                    from_owner[0], from_owner[1], currency_id,
                ),
            )
        to_before = to_after = 0
        if to_owner:
            self._ensure_wallet(
                connection, session_id, to_owner[0], to_owner[1],
                currency_id,
            )
            to_before = wallet_balance(to_owner)
            to_after = to_before + minor
            connection.execute(
                """
                UPDATE economy_wallets SET balance = ?, updated_at = ?
                WHERE session_id = ? AND owner_type = ? AND owner_ref = ?
                  AND currency_id = ?
                """,
                (
                    to_after, utc_now(), session_id,
                    to_owner[0], to_owner[1], currency_id,
                ),
            )
        now = utc_now()
        tx_id = new_id("etx")
        connection.execute(
            """
            INSERT INTO economy_transactions(
                id, session_id, operation_id, kind, currency_id,
                from_owner_type, from_owner_ref, to_owner_type,
                to_owner_ref, amount, balance_before, balance_after,
                reason, source, actor_id, target_ref, event_id,
                status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'committed', ?)
            """,
            (
                tx_id, session_id, operation_id, kind, currency_id,
                (from_owner[0] if from_owner else ""),
                (from_owner[1] if from_owner else ""),
                (to_owner[0] if to_owner else ""),
                (to_owner[1] if to_owner else ""),
                minor, from_before, from_after,
                reason, source, actor_id, target_ref, event_id, now,
            ),
        )
        self._insert_audit(
            connection,
            session_id,
            actor_id or source,
            "economy.apply",
            tx_id,
            {
                "operation_id": operation_id,
                "kind": kind,
                "currency_id": currency_id,
                "amount": minor,
                "from": from_owner,
                "to": to_owner,
                "source": source,
            },
        )
        self._enqueue_storage_sync(connection, [session_id], "sync")
        return {
            "ok": True,
            "operation_id": operation_id,
            "transaction_id": tx_id,
            "session_id": session_id,
            "kind": kind,
            "currency_id": currency_id,
            "amount": minor,
            "amount_major": _minor_to_major(minor, precision),
            "balance_before": from_before,
            "balance_after": from_after,
            "actor_id": actor_id,
            "created_at": now,
        }

    def _tx_result(self, row: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "operation_id": row["operation_id"],
            "transaction_id": row["id"],
            "session_id": row["session_id"],
            "kind": row["kind"],
            "currency_id": row["currency_id"],
            "amount": row["amount"],
            "balance_before": row["balance_before"],
            "balance_after": row["balance_after"],
            "actor_id": row["actor_id"],
            "created_at": row["created_at"],
            "idempotent_replay": True,
        }

    async def economy_transfer(
        self,
        *,
        session_id: str,
        operation_id: str,
        currency_id: str,
        from_owner: tuple[str, str],
        to_owner: tuple[str, str],
        amount: Any,
        reason: str = "",
        source: str = ECONOMY_SOURCE_WEB,
        actor_id: str = "",
    ) -> dict[str, Any]:
        return await self.economy_apply(
            session_id=session_id,
            operation_id=operation_id,
            kind="transfer",
            currency_id=currency_id,
            amount=amount,
            from_owner=from_owner,
            to_owner=to_owner,
            reason=reason,
            source=source,
            actor_id=actor_id,
        )

    async def economy_list_transactions(
        self,
        session_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return await self._run(
            self._economy_list_transactions, session_id, int(limit)
        )

    def _economy_list_transactions(
        self,
        session_id: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        limit = min(500, max(1, int(limit)))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM economy_transactions
                WHERE session_id = ?
                ORDER BY created_at DESC, rowid DESC LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]


    # ── 1.0.0-A7：角色初始钱包播种（建卡确认时调用，幂等） ───────────