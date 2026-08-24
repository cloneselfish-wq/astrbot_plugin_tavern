from __future__ import annotations

from .economy_support import *


class CurrencyExchangeRepositoryMixin:
    async def economy_exchange(
        self,
        *,
        session_id: str,
        operation_id: str,
        currency_id: str,
        from_owner: tuple[str, str],
        to_owner: tuple[str, str, str],
        amount: Any,
        source: str = ECONOMY_SOURCE_WEB,
        actor_id: str = "",
    ) -> dict[str, Any]:
        """按兑换规则把 amount 主单位的 from 货币换成 to 货币（受汇率约束）。"""
        plan = await self._run(
            self._economy_exchange_plan,
            session_id,
            str(operation_id or "").strip(),
            str(currency_id or "").strip(),
            from_owner,
            to_owner,
            amount,
        )
        if plan.get("replay"):
            return plan
        to_currency = plan["to_currency"]
        from_minor = plan["from_minor"]
        to_minor = plan["to_minor"]
        from_precision = plan["from_precision"]
        to_precision = plan["to_precision"]
        numerator = plan["numerator"]
        denominator = plan["denominator"]
        fee = plan["fee"]
        debit = await self.economy_apply(
            session_id=session_id,
            operation_id=f"{operation_id}:debit",
            kind="exchange",
            currency_id=currency_id,
            amount=_minor_to_major(from_minor, from_precision),
            from_owner=(from_owner[0], from_owner[1]),
            reason="exchange",
            source=source,
            actor_id=actor_id,
        )
        if not debit.get("ok"):
            return debit
        credit = await self.economy_apply(
            session_id=session_id,
            operation_id=f"{operation_id}:credit",
            kind="exchange",
            currency_id=to_currency,
            amount=_minor_to_major(to_minor, to_precision),
            to_owner=(to_owner[0], to_owner[1]),
            reason="exchange",
            source=source,
            actor_id=actor_id,
        )
        if not credit.get("ok"):
            await self.economy_apply(
                session_id=session_id,
                operation_id=f"{operation_id}:refund",
                kind="exchange_refund",
                currency_id=currency_id,
                amount=_minor_to_major(from_minor, from_precision),
                to_owner=(from_owner[0], from_owner[1]),
                reason="exchange_refund",
                source=source,
                actor_id=actor_id,
            )
            return {
                **credit,
                "message": "兑换入账失败，已退回来源："
                + str(credit.get("message", "")),
            }
        return {
            **credit,
            "from_currency": currency_id,
            "to_currency": to_currency,
            "from_amount": _minor_to_major(from_minor, from_precision),
            "to_amount": _minor_to_major(to_minor, to_precision),
            "rate": f"{numerator}/{denominator}",
            "fee": fee,
        }

    def _economy_exchange_plan(
        self,
        session_id: str,
        operation_id: str,
        currency_id: str,
        from_owner: tuple[str, str],
        to_owner: tuple[str, str, str],
        amount: Any,
    ) -> dict[str, Any]:
        to_currency = to_owner[2] if len(to_owner) >= 3 else ""
        if not to_currency:
            raise ValueError("兑换必须指定目标货币")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._require_enabled(connection, session_id)
                existing = connection.execute(
                    """
                    SELECT * FROM economy_transactions
                    WHERE session_id = ? AND operation_id = ?
                    """,
                    (session_id, operation_id),
                ).fetchone()
                if existing:
                    connection.execute("COMMIT")
                    return self._tx_result(existing)
                rule = connection.execute(
                    """
                    SELECT rate_numerator, rate_denominator, fee, enabled
                    FROM economy_exchange_rules
                    WHERE session_id = ? AND from_currency = ? AND to_currency = ?
                    """,
                    (session_id, currency_id, to_currency),
                ).fetchone()
                if not rule or not rule["enabled"]:
                    raise ValueError(
                        f"该货币不可兑换：{currency_id} → {to_currency}"
                    )
                from_precision = self._currency_precision(
                    connection, session_id, currency_id
                )
                to_precision = self._currency_precision(
                    connection, session_id, to_currency
                )
                from_minor = _major_to_minor(amount, from_precision)
                numerator = int(rule["rate_numerator"])
                denominator = int(rule["rate_denominator"] or 1)
                # 汇率按主单位结算，再换算到目标货币的最小单位
                from_major = Decimal(from_minor) / Decimal(10 ** from_precision)
                to_major = (
                    from_major * Decimal(numerator) / Decimal(denominator)
                )
                to_minor = int(to_major * Decimal(10 ** to_precision))
                fee = int(rule["fee"] or 0)
                to_minor = max(0, to_minor - fee)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return {
            "to_currency": to_currency,
            "from_minor": from_minor,
            "to_minor": to_minor,
            "from_precision": from_precision,
            "to_precision": to_precision,
            "numerator": numerator,
            "denominator": denominator,
            "fee": fee,
        }
