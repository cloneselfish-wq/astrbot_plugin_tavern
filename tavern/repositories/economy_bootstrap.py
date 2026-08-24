from __future__ import annotations

from .economy_support import *


class EconomyBootstrapRepositoryMixin:
    async def set_economy_enabled(
        self,
        session_id: str,
        enabled: bool,
        actor_id: str,
    ) -> dict[str, Any]:
        result = await self._run(
            self._set_economy_enabled, session_id, bool(enabled), actor_id
        )
        if enabled:
            try:
                created = await self.ensure_economy_currencies(session_id)
                result["seeded_count"] = int(created)
            except Exception as exc:
                logger.exception("321开团启用经济后初始化货币失败")
                result["seed_error"] = {
                    "code": "economy.seed_failed",
                    "message": clean_text(str(exc), max_chars=300)
                    or "经济定义播种失败",
                    "retryable": True,
                }
        result["capability"] = await self.economy_capability(session_id)
        return result

    def _set_economy_enabled(
        self,
        session_id: str,
        enabled: bool,
        actor_id: str,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                connection.execute(
                    """
                    INSERT INTO economy_state(session_id, enabled, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        enabled = excluded.enabled,
                        updated_at = excluded.updated_at
                    """,
                    (session_id, int(enabled), now),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "economy.enabled",
                    "",
                    {"enabled": enabled},
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return {"session_id": session_id, "enabled": enabled}

    async def economy_capability(
        self,
        session_id: str,
    ) -> dict[str, Any]:
        return await self._run(self._economy_capability, session_id)

    def _economy_capability(self, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            state = connection.execute(
                "SELECT enabled FROM economy_state WHERE session_id=?",
                (session_id,),
            ).fetchone()
            config = connection.execute(
                """
                SELECT ic.world_snapshot_json, ic.world_revision,
                       w.id AS installed_world_ref,
                       w.revision AS installed_world_revision,
                       w.content_version AS installed_content_version
                FROM instance_configs ic
                JOIN sessions s ON s.id=ic.session_id
                LEFT JOIN worlds w ON w.id=s.world_id
                WHERE ic.session_id=?
                """,
                (session_id,),
            ).fetchone()
            world = json_load(
                config["world_snapshot_json"] if config else "",
                {},
            )
            rules = world.get("rules")
            rules = rules if isinstance(rules, Mapping) else {}
            economy = rules.get("economy")
            declared = isinstance(economy, Mapping) and bool(economy)
            economy = economy if isinstance(economy, Mapping) else {}
            definitions = {
                "currencies": len(
                    economy.get("currencies")
                    if isinstance(economy.get("currencies"), list)
                    else []
                ),
                "shops": len(
                    economy.get("shops")
                    if isinstance(economy.get("shops"), list)
                    else []
                ),
                "exchange_rules": len(
                    economy.get("exchange_rules")
                    if isinstance(economy.get("exchange_rules"), list)
                    else []
                ),
            }
            persisted = {
                "currencies": int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM economy_currencies
                        WHERE session_id=?
                        """,
                        (session_id,),
                    ).fetchone()[0]
                ),
                "wallets": int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM economy_wallets
                        WHERE session_id=?
                        """,
                        (session_id,),
                    ).fetchone()[0]
                ),
                "exchange_rules": int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM economy_exchange_rules
                        WHERE session_id=?
                        """,
                        (session_id,),
                    ).fetchone()[0]
                ),
            }
            receipt = connection.execute(
                """
                SELECT status, result_json FROM operation_receipts
                WHERE session_id=? AND operation_type='economy.seed'
                ORDER BY updated_at DESC, created_at DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        frozen_world_revision = int(
            config["world_revision"] if config else 0
        )
        installed_world_revision = int(
            config["installed_world_revision"] if config else 0
        )
        frozen_content_version = str(
            world.get("content_version") or ""
        )
        installed_content_version = str(
            config["installed_content_version"] if config else ""
        )
        if installed_world_revision <= 0:
            snapshot_state = "detached"
        elif (
            installed_world_revision > frozen_world_revision
            or (
                frozen_content_version
                and installed_content_version
                and frozen_content_version != installed_content_version
            )
        ):
            snapshot_state = "legacy_snapshot"
        else:
            snapshot_state = "current"
        enabled = bool(state and state["enabled"])
        receipt_result = (
            json_load(receipt["result_json"], {})
            if receipt is not None
            else {}
        )
        issue = None
        if not declared:
            seed_state = "not_applicable"
            enabled = False
        elif receipt is not None and str(receipt["status"]) == "failed_retryable":
            seed_state = "error"
            issue = {
                "code": "economy.seed_failed",
                "message": str(
                    receipt_result.get("message")
                    or "经济定义播种失败，请重试。"
                ),
                "retryable": True,
            }
        elif (
            persisted["currencies"] >= definitions["currencies"]
            and definitions["currencies"] > 0
        ):
            seed_state = "seeded"
        else:
            seed_state = "not_started"
        actions: list[str] = ["diagnose"]
        if snapshot_state == "legacy_snapshot":
            actions.extend(("keep_frozen", "clone_to_latest"))
        if declared and enabled:
            actions.append("disable")
        elif declared:
            actions.append("enable")
        if seed_state == "error":
            actions.append("retry_seed")
        return {
            "schema": "tavern-economy-capability/1.0.0-rc10",
            "declared": declared,
            "enabled": enabled,
            "seed_state": seed_state,
            "snapshot_state": snapshot_state,
            "frozen_world_revision": frozen_world_revision,
            "installed_world_revision": installed_world_revision,
            "frozen_content_version": frozen_content_version,
            "installed_content_version": installed_content_version,
            "installed_world_ref": str(
                config["installed_world_ref"] if config else ""
            ),
            "definition_counts": definitions,
            "persisted_counts": persisted,
            "issue": issue,
            "available_actions": actions,
        }

    def _require_enabled(self, connection: Any, session_id: str) -> None:
        row = connection.execute(
            "SELECT enabled FROM economy_state WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if not row or not row["enabled"]:
            raise EconomyDisabledError("经济系统未启用")

    def _ensure_wallet(
        self,
        connection: Any,
        session_id: str,
        owner_type: str,
        owner_ref: str,
        currency_id: str,
        *,
        initial: int = 0,
    ) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO economy_wallets(
                id, session_id, owner_type, owner_ref, currency_id,
                balance, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("wallet"),
                session_id,
                str(owner_type or "party"),
                str(owner_ref or ""),
                currency_id,
                int(initial),
                utc_now(),
            ),
        )

    # ── 货币定义（懒播种） ────────────────────────────────────────
    async def ensure_economy_currencies(self, session_id: str) -> int:
        try:
            created = await self._run(
                self._ensure_economy_currencies,
                session_id,
            )
        except Exception as exc:
            await self._run(
                self._record_economy_seed_receipt,
                session_id,
                "failed_retryable",
                0,
                clean_text(str(exc), max_chars=300),
            )
            raise
        await self._run(
            self._record_economy_seed_receipt,
            session_id,
            "completed",
            int(created),
            "",
        )
        return int(created)

    def _record_economy_seed_receipt(
        self,
        session_id: str,
        status: str,
        created: int,
        message: str,
    ) -> None:
        now = utc_now()
        with self._connect() as connection:
            config = connection.execute(
                """
                SELECT world_snapshot_json FROM instance_configs
                WHERE session_id=?
                """,
                (session_id,),
            ).fetchone()
            snapshot = str(
                config["world_snapshot_json"] if config else ""
            )
            snapshot_hash = hashlib.sha256(
                snapshot.encode("utf-8")
            ).hexdigest()
            operation_id = f"economy-seed:{session_id}:{snapshot_hash[:16]}"
            result = {
                "created": int(created),
                "message": str(message or ""),
                "snapshot_hash": snapshot_hash,
            }
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO operation_receipts(
                        operation_id, session_id, operation_type,
                        request_json, result_json, status, phase,
                        input_hash, created_at, updated_at
                    ) VALUES (?, ?, 'economy.seed', ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(operation_id) DO UPDATE SET
                        result_json=excluded.result_json,
                        status=excluded.status,
                        phase=excluded.phase,
                        updated_at=excluded.updated_at
                    """,
                    (
                        operation_id,
                        session_id,
                        json_dump({"snapshot_hash": snapshot_hash}),
                        json_dump(result),
                        status,
                        (
                            "committed"
                            if status == "completed"
                            else "seed_failed"
                        ),
                        snapshot_hash,
                        now,
                        now,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _ensure_economy_currencies(self, session_id: str) -> int:
        """从冻结世界快照 `rules.economy` 播种货币与兑换规则（幂等）。"""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT world_snapshot_json FROM instance_configs WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if not row:
                return 0
            world = json_load(row["world_snapshot_json"], {})
        rules = world.get("rules") if isinstance(world, Mapping) else {}
        economy = rules.get("economy") if isinstance(rules, Mapping) else {}
        if not isinstance(economy, Mapping) or not economy:
            return 0
        currencies = economy.get("currencies") or []
        if not isinstance(currencies, list):
            return 0
        now = utc_now()
        created = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                existing = {
                    str(r[0])
                    for r in connection.execute(
                        "SELECT currency_id FROM economy_currencies WHERE session_id = ?",
                        (session_id,),
                    ).fetchall()
                }
                for index, raw in enumerate(currencies):
                    if not isinstance(raw, Mapping):
                        continue
                    currency_id = str(
                        raw.get("currency_id") or raw.get("id") or ""
                    ).strip()
                    name = str(raw.get("name") or currency_id).strip()
                    if not currency_id or currency_id in existing:
                        continue
                    connection.execute(
                        """
                        INSERT INTO economy_currencies(
                            id, session_id, currency_id, name, short_name, icon,
                            description, precision, allow_negative, transferable,
                            exchangeable, public, sort_order, extensions_json,
                            created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            new_id("ecur"),
                            session_id,
                            currency_id,
                            name,
                            str(raw.get("short_name") or "").strip(),
                            str(raw.get("icon") or "").strip(),
                            str(raw.get("description") or "").strip(),
                            int(raw.get("precision") or 0),
                            int(bool(raw.get("allow_negative", False))),
                            int(bool(raw.get("transferable", True))),
                            int(bool(raw.get("exchangeable", False))),
                            int(bool(raw.get("public", True))),
                            int(raw.get("sort_order") or index),
                            json_dump(raw.get("extensions") or {}),
                            now,
                        ),
                    )
                    created += 1
                    existing.add(currency_id)
                wallets = economy.get("initial_wallets") or []
                if isinstance(wallets, list):
                    for item in wallets:
                        if not isinstance(item, Mapping):
                            continue
                        owner_type = str(item.get("owner_type") or "party")
                        owner_ref = str(item.get("owner_ref") or "").strip()
                        cid = str(item.get("currency_id") or "").strip()
                        if not owner_ref or not cid:
                            continue
                        if owner_ref.startswith("{"):
                            # 占位符（如 {self}）由建卡确认时按参与者展开，此处跳过。
                            continue
                        precision = self._currency_precision(
                            connection, session_id, cid
                        )
                        self._ensure_wallet(
                            connection,
                            session_id,
                            owner_type,
                            owner_ref,
                            cid,
                            initial=_major_to_minor(
                                item.get("amount") or 0, precision
                            ),
                        )
                rules_list = economy.get("exchange_rules") or []
                if isinstance(rules_list, list):
                    for item in rules_list:
                        if not isinstance(item, Mapping):
                            continue
                        frm = str(item.get("from") or item.get("from_currency") or "")
                        to = str(item.get("to") or item.get("to_currency") or "")
                        if not frm or not to:
                            continue
                        connection.execute(
                            """
                            INSERT OR IGNORE INTO economy_exchange_rules(
                                id, session_id, from_currency, to_currency,
                                rate_numerator, rate_denominator, fee, enabled,
                                created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                            """,
                            (
                                new_id("exch"),
                                session_id,
                                frm,
                                to,
                                int(item.get("rate_numerator") or 1),
                                int(item.get("rate_denominator") or 1),
                                int(item.get("fee") or 0),
                                now,
                            ),
                        )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return created

    # ── 查询 ──────────────────────────────────────────────────────
    async def seed_starter_wallet(
        self,
        session_id: str,
        participant_ref: str,
        world: Mapping[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        """把 rules.economy.initial_wallets 中 owner_type 为 character 的
        初始金额写到该角色钱包（owner_ref=participant id）。仅创建不覆盖。"""
        from ..world_contract import world_contract

        contract = world_contract(world) if isinstance(world, Mapping) else {}
        economy = contract.get("economy") or {}
        initial_wallets = economy.get("initial_wallets") or []
        if not initial_wallets:
            return {"ok": True, "seeded": []}
        return await self._run(
            self._seed_starter_wallet,
            session_id,
            clean_text(participant_ref, max_chars=128),
            [dict(item) for item in initial_wallets if isinstance(item, Mapping)],
            str(actor_id or "").strip(),
        )

    def _seed_starter_wallet(
        self,
        session_id: str,
        participant_ref: str,
        initial_wallets: list[dict[str, Any]],
        actor_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                row = connection.execute(
                    """
                    SELECT * FROM participants
                    WHERE session_id = ? AND (
                        id = ? OR group_user_id = ? OR
                        lower(character_name) = lower(?) OR
                        lower(character_code) = lower(?) OR
                        lower(display_name) = lower(?)
                    )
                    ORDER BY CASE WHEN id = ? THEN 0 ELSE 1 END
                    LIMIT 1
                    """,
                    (
                        session_id,
                        participant_ref,
                        participant_ref,
                        participant_ref,
                        participant_ref,
                        participant_ref,
                        participant_ref,
                    ),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("角色不存在，无法发放初始资金")
                seeded: list[str] = []
                for item in initial_wallets:
                    owner_type = str(
                        item.get("owner_type") or "character"
                    ).strip().lower()
                    if owner_type not in {"character", "player"}:
                        continue
                    cid = str(item.get("currency_id") or "").strip()
                    if not cid:
                        continue
                    precision = self._currency_precision(
                        connection, session_id, cid
                    )
                    amount = _major_to_minor(item.get("amount") or 0, precision)
                    if amount <= 0:
                        continue
                    self._ensure_wallet(
                        connection,
                        session_id,
                        "character",
                        str(row["id"]),
                        cid,
                        initial=amount,
                    )
                    seeded.append(
                        f"{cid} {_minor_to_major(amount, precision)}"
                    )
                if seeded:
                    self._insert_audit(
                        connection,
                        session_id,
                        actor_id,
                        "economy.starter_wallet",
                        row["id"],
                        {"seeded": seeded},
                    )
                connection.execute("COMMIT")
                return {
                    "ok": True,
                    "participant_id": row["id"],
                    "character_name": row["character_name"] or row["display_name"],
                    "seeded": seeded,
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise
