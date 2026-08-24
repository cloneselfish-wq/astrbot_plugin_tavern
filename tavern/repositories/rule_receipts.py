from __future__ import annotations

from .rules_support import *


class RuleReceiptsRepositoryMixin:
    async def get_resolution_receipt(
        self, receipt_id: str, *, public_only: bool = False
    ) -> dict[str, Any]:
        return await self._run(
            self._get_resolution_receipt, receipt_id, bool(public_only)
        )

    def _get_resolution_receipt(
        self, receipt_id: str, public_only: bool
    ) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM resolution_receipts WHERE receipt_id=?",
                (receipt_id,),
            ).fetchone()
            if not row:
                raise DatabaseNotFoundError("裁定凭证不存在")
            return json_load(
                row["public_projection_json"] if public_only else row["receipt_json"], {}
            )

    async def reserve_token_usage(
        self,
        session_id: str,
        request_type: str,
        provider_id: str,
        expected_tokens: int,
    ) -> dict[str, Any]:
        return await self._run(
            self._reserve_token_usage,
            session_id,
            request_type,
            provider_id,
            expected_tokens,
        )

    def _reserve_token_usage(
        self,
        session_id: str,
        request_type: str,
        provider_id: str,
        expected_tokens: int,
    ) -> dict[str, Any]:
        expected_tokens = bounded_int(
            expected_tokens,
            1,
            1,
            10_000_000,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute(
                    """
                    SELECT id, group_id FROM sessions WHERE id = ?
                    """,
                    (session_id,),
                ).fetchone()
                if not session:
                    raise DatabaseNotFoundError("副本不存在")
                now_dt = datetime.now(timezone.utc)
                now = now_dt.isoformat(timespec="seconds")
                stale = (
                    now_dt - timedelta(minutes=15)
                ).isoformat(timespec="seconds")
                connection.execute(
                    """
                    UPDATE token_usage SET status = 'failed',
                        settled_at = ?
                    WHERE status = 'reserved' AND created_at < ?
                    """,
                    (now, stale),
                )
                policies = connection.execute(
                    """
                    SELECT * FROM token_quota_policies
                    WHERE enabled = 1 AND (
                        (scope_type = 'group' AND scope_id = ?)
                        OR (scope_type = 'session' AND scope_id = ?)
                    )
                    """,
                    (session["group_id"], session_id),
                ).fetchall()
                quota_status: list[dict[str, Any]] = []
                for policy in policies:
                    cutoff = (
                        now_dt - timedelta(
                            seconds=int(policy["window_seconds"])
                        )
                    ).isoformat(timespec="seconds")
                    if policy["scope_type"] == "group":
                        used = int(
                            connection.execute(
                                """
                                SELECT COALESCE(SUM(
                                    CASE
                                      WHEN status = 'completed'
                                      THEN total_tokens
                                      WHEN status = 'reserved'
                                      THEN reserved_tokens
                                      ELSE 0
                                    END
                                ), 0)
                                FROM token_usage
                                WHERE group_id = ? AND created_at >= ?
                                """,
                                (session["group_id"], cutoff),
                            ).fetchone()[0]
                        )
                    else:
                        used = int(
                            connection.execute(
                                """
                                SELECT COALESCE(SUM(
                                    CASE
                                      WHEN status = 'completed'
                                      THEN total_tokens
                                      WHEN status = 'reserved'
                                      THEN reserved_tokens
                                      ELSE 0
                                    END
                                ), 0)
                                FROM token_usage
                                WHERE session_id = ? AND created_at >= ?
                                """,
                                (session_id, cutoff),
                            ).fetchone()[0]
                        )
                    remaining = max(0, int(policy["token_limit"]) - used)
                    quota_status.append(
                        {
                            "scope_type": policy["scope_type"],
                            "used": used,
                            "limit": int(policy["token_limit"]),
                            "remaining": remaining,
                            "window_seconds": int(
                                policy["window_seconds"]
                            ),
                        }
                    )
                    if expected_tokens > remaining:
                        label = (
                            "群"
                            if policy["scope_type"] == "group"
                            else "副本"
                        )
                        raise ValueError(
                            f"{label} Token 限额不足：当前窗口剩余 "
                            f"{remaining}，本次最多需要 {expected_tokens}"
                        )
                usage_id = new_id("usage")
                connection.execute(
                    """
                    INSERT INTO token_usage(
                        id, session_id, group_id, request_type, provider_id,
                        reserved_tokens, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'reserved', ?)
                    """,
                    (
                        usage_id,
                        session_id,
                        session["group_id"],
                        clean_text(request_type, max_chars=64),
                        clean_text(provider_id, max_chars=200),
                        expected_tokens,
                        now,
                    ),
                )
                connection.execute("COMMIT")
                return {
                    "id": usage_id,
                    "session_id": session_id,
                    "group_id": session["group_id"],
                    "reserved_tokens": expected_tokens,
                    "quotas": quota_status,
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def settle_token_usage(
        self,
        usage_id: str,
        *,
        input_tokens: int,
        cached_input_tokens: int,
        output_tokens: int,
        usage_source: str,
    ) -> None:
        await self._run(
            self._settle_token_usage,
            usage_id,
            input_tokens,
            cached_input_tokens,
            output_tokens,
            usage_source,
        )

    def _settle_token_usage(
        self,
        usage_id: str,
        input_tokens: int,
        cached_input_tokens: int,
        output_tokens: int,
        usage_source: str,
    ) -> None:
        input_tokens = max(0, int(input_tokens or 0))
        cached_input_tokens = max(
            0,
            min(input_tokens, int(cached_input_tokens or 0)),
        )
        output_tokens = max(0, int(output_tokens or 0))
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE token_usage SET
                    input_tokens = ?, cached_input_tokens = ?,
                    output_tokens = ?, total_tokens = ?,
                    usage_source = ?, status = 'completed',
                    settled_at = ?
                WHERE id = ? AND status = 'reserved'
                """,
                (
                    input_tokens,
                    cached_input_tokens,
                    output_tokens,
                    input_tokens + output_tokens,
                    clean_text(usage_source, max_chars=32) or "estimated",
                    utc_now(),
                    usage_id,
                ),
            )

    async def fail_token_usage(self, usage_id: str) -> None:
        await self._run(self._fail_token_usage, usage_id)

    def _fail_token_usage(self, usage_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE token_usage
                SET status = 'failed', settled_at = ?
                WHERE id = ? AND status = 'reserved'
                """,
                (utc_now(), usage_id),
            )

    async def token_usage_summary(
        self,
        session_id: str,
    ) -> dict[str, Any]:
        return await self._run(self._token_usage_summary, session_id)

    def _token_usage_summary(self, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            session = connection.execute(
                """
                SELECT id, group_id FROM sessions WHERE id = ?
                """,
                (session_id,),
            ).fetchone()
            if not session:
                raise DatabaseNotFoundError("副本不存在")
            now_dt = datetime.now(timezone.utc)

            def total(where: str, value: str, seconds: int | None) -> int:
                parameters: list[Any] = [value]
                cutoff_sql = ""
                if seconds is not None:
                    cutoff_sql = " AND created_at >= ?"
                    parameters.append(
                        (
                            now_dt - timedelta(seconds=seconds)
                        ).isoformat(timespec="seconds")
                    )
                return int(
                    connection.execute(
                        f"""
                        SELECT COALESCE(SUM(total_tokens), 0)
                        FROM token_usage
                        WHERE {where} = ? AND status = 'completed'
                        {cutoff_sql}
                        """,
                        tuple(parameters),
                    ).fetchone()[0]
                )

            policies = connection.execute(
                """
                SELECT * FROM token_quota_policies
                WHERE (scope_type = 'group' AND scope_id = ?)
                   OR (scope_type = 'session' AND scope_id = ?)
                ORDER BY scope_type
                """,
                (session["group_id"], session_id),
            ).fetchall()
            quota_items: list[dict[str, Any]] = []
            for row in policies:
                scope_column = (
                    "group_id"
                    if row["scope_type"] == "group"
                    else "session_id"
                )
                scope_value = (
                    session["group_id"]
                    if row["scope_type"] == "group"
                    else session_id
                )
                used = total(
                    scope_column,
                    str(scope_value),
                    int(row["window_seconds"]),
                )
                quota_items.append(
                    {
                        "id": row["id"],
                        "scope_type": row["scope_type"],
                        "scope_id": row["scope_id"],
                        "window_seconds": int(row["window_seconds"]),
                        "token_limit": int(row["token_limit"]),
                        "enabled": bool(row["enabled"]),
                        "used": used,
                        "remaining": max(
                            0,
                            int(row["token_limit"]) - used,
                        ),
                        "revision": int(row["revision"]),
                    }
                )
            by_type = [
                {
                    "request_type": row["request_type"],
                    "tokens": int(row["tokens"]),
                    "requests": int(row["requests"]),
                }
                for row in connection.execute(
                    """
                    SELECT request_type, SUM(total_tokens) AS tokens,
                           COUNT(*) AS requests
                    FROM token_usage
                    WHERE session_id = ? AND status = 'completed'
                    GROUP BY request_type
                    ORDER BY tokens DESC
                    """,
                    (session_id,),
                ).fetchall()
            ]
            return {
                "session_id": session_id,
                "group_id": session["group_id"],
                "session": {
                    "hour": total("session_id", session_id, 3600),
                    "day": total("session_id", session_id, 86400),
                    "all": total("session_id", session_id, None),
                },
                "group": {
                    "hour": total(
                        "group_id",
                        str(session["group_id"]),
                        3600,
                    ),
                    "day": total(
                        "group_id",
                        str(session["group_id"]),
                        86400,
                    ),
                    "all": total(
                        "group_id",
                        str(session["group_id"]),
                        None,
                    ),
                },
                "quotas": quota_items,
                "by_type": by_type,
            }

    async def group_token_usage_summary(
        self,
        platform_id: str,
        group_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._group_token_usage_summary,
            platform_id,
            group_id,
        )

    def _group_token_usage_summary(
        self,
        platform_id: str,
        group_id: str,
    ) -> dict[str, Any]:
        platform_id = validate_platform_id(
            platform_id,
            label="平台实例 ID",
        )
        group_id = validate_platform_id(group_id, label="群 ID")
        with self._connect() as connection:
            session = connection.execute(
                """
                SELECT id FROM sessions
                WHERE platform_id = ? AND group_id = ?
                ORDER BY selected DESC, updated_at DESC
                LIMIT 1
                """,
                (platform_id, group_id),
            ).fetchone()
        if not session:
            raise DatabaseNotFoundError("群会话不存在")
        usage = self._token_usage_summary(str(session["id"]))
        group_quota = next(
            (
                item
                for item in usage["quotas"]
                if item["scope_type"] == "group"
            ),
            None,
        )
        return {
            "platform_id": platform_id,
            "group_id": group_id,
            "session_id": str(session["id"]),
            "group": usage["group"],
            "quota": group_quota,
        }

    async def token_quota_contexts(
        self,
        session_ids: Sequence[str],
    ) -> dict[str, dict[str, Any]]:
        return await self._run(
            self._token_quota_contexts,
            [clean_text(item, max_chars=240) for item in session_ids],
        )

    def _token_quota_contexts(
        self,
        session_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        values = list(dict.fromkeys(item for item in session_ids if item))[:100]
        if not values:
            return {}
        placeholders = ",".join("?" for _ in values)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT s.id AS session_id, q.window_seconds,
                       q.token_limit, q.enabled, q.revision, q.updated_at
                FROM sessions s
                LEFT JOIN token_quota_policies q
                  ON q.scope_type='group' AND q.scope_id=s.group_id
                WHERE s.id IN ({placeholders})
                """,
                tuple(values),
            ).fetchall()
        return {
            str(row["session_id"]): {
                "window_seconds": int(row["window_seconds"] or 86400),
                "token_limit": int(row["token_limit"] or 400000),
                "enabled": bool(row["enabled"] or 0),
                "revision": int(row["revision"] or 0),
                "updated_at": str(row["updated_at"] or ""),
            }
            for row in rows
        }

    async def set_token_quota(
        self,
        session_id: str,
        scope_type: str,
        *,
        window_seconds: int,
        token_limit: int,
        enabled: bool,
        actor_id: str,
        expected_revision: int | None = None,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        return await self._run(
            self._set_token_quota,
            session_id,
            scope_type,
            window_seconds,
            token_limit,
            enabled,
            actor_id,
            expected_revision,
            idempotency_key,
        )

    def _set_token_quota(
        self,
        session_id: str,
        scope_type: str,
        window_seconds: int,
        token_limit: int,
        enabled: bool,
        actor_id: str,
        expected_revision: int | None = None,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        scope_type = str(scope_type or "").strip().lower()
        if scope_type not in {"group", "session"}:
            raise ValueError("限额范围必须为群或副本")
        window_seconds = bounded_int(
            window_seconds,
            3600,
            60,
            365 * 24 * 60 * 60,
        )
        token_limit = bounded_int(
            token_limit,
            100_000,
            1,
            1_000_000_000,
        )
        request_key = clean_text(idempotency_key, max_chars=240)
        request_payload = {
            "session_id": clean_text(session_id, max_chars=240),
            "scope_type": scope_type,
            "window_seconds": window_seconds,
            "token_limit": token_limit,
            "enabled": bool(enabled),
            "expected_revision": expected_revision,
        }
        input_hash = content_hash(request_payload)
        operation_result: dict[str, Any] | None = None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if request_key:
                    receipt = connection.execute(
                        "SELECT * FROM operation_commits WHERE operation_id=?",
                        (request_key,),
                    ).fetchone()
                    if receipt is not None:
                        if str(receipt["input_hash"] or "") != input_hash:
                            raise DatabaseConflictError(
                                "相同幂等键已用于另一份 Token 限额修改"
                            )
                        if str(receipt["status"] or "") == "completed":
                            replay = json_load(receipt["result_json"], {})
                            replay["replayed"] = True
                            connection.execute("COMMIT")
                            return replay
                        raise DatabaseConflictError(
                            "Token 限额修改仍在处理中，请稍后重试"
                        )
                self._assert_session_writable(connection, session_id)
                session = connection.execute(
                    "SELECT group_id FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not session:
                    raise DatabaseNotFoundError("副本不存在")
                scope_id = (
                    str(session["group_id"])
                    if scope_type == "group"
                    else session_id
                )
                current = connection.execute(
                    """
                    SELECT * FROM token_quota_policies
                    WHERE scope_type=? AND scope_id=?
                    """,
                    (scope_type, scope_id),
                ).fetchone()
                current_revision = int(current["revision"] or 0) if current else 0
                if (
                    expected_revision is not None
                    and current_revision != int(expected_revision)
                ):
                    raise DatabaseConflictError("Token 限额修订已经变化")
                now = utc_now()
                connection.execute(
                    """
                    INSERT INTO token_quota_policies(
                        id, scope_type, scope_id, window_seconds,
                        token_limit, enabled, revision, updated_by, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(scope_type, scope_id) DO UPDATE SET
                        window_seconds = excluded.window_seconds,
                        token_limit = excluded.token_limit,
                        enabled = excluded.enabled,
                        revision = token_quota_policies.revision + 1,
                        updated_by = excluded.updated_by,
                        updated_at = excluded.updated_at
                    """,
                    (
                        new_id("quota"),
                        scope_type,
                        scope_id,
                        window_seconds,
                        token_limit,
                        int(bool(enabled)),
                        actor_id,
                        now,
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "token.quota",
                    scope_type,
                    {
                        "window_seconds": window_seconds,
                        "token_limit": token_limit,
                        "enabled": bool(enabled),
                    },
                )
                updated = connection.execute(
                    """
                    SELECT * FROM token_quota_policies
                    WHERE scope_type=? AND scope_id=?
                    """,
                    (scope_type, scope_id),
                ).fetchone()
                operation_result = {
                    "scope_type": scope_type,
                    "window_seconds": int(updated["window_seconds"]),
                    "token_limit": int(updated["token_limit"]),
                    "enabled": bool(updated["enabled"]),
                    "revision": int(updated["revision"]),
                    "updated_at": str(updated["updated_at"] or now),
                }
                if request_key:
                    connection.execute(
                        """
                        INSERT INTO operation_commits(
                            operation_id, session_id, input_hash, status,
                            result_json, rollback_json, created_at, updated_at
                        ) VALUES (?, ?, ?, 'completed', ?, '{}', ?, ?)
                        """,
                        (
                            request_key,
                            session_id,
                            input_hash,
                            json_dump(operation_result),
                            now,
                            now,
                        ),
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return operation_result if request_key else self._token_usage_summary(session_id)

    async def set_group_token_quota(
        self,
        platform_id: str,
        group_id: str,
        *,
        window_seconds: int,
        token_limit: int,
        enabled: bool,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._set_group_token_quota,
            platform_id,
            group_id,
            window_seconds,
            token_limit,
            enabled,
            actor_id,
        )

    def _set_group_token_quota(
        self,
        platform_id: str,
        group_id: str,
        window_seconds: int,
        token_limit: int,
        enabled: bool,
        actor_id: str,
    ) -> dict[str, Any]:
        platform_id = validate_platform_id(
            platform_id,
            label="平台实例 ID",
        )
        group_id = validate_platform_id(group_id, label="群 ID")
        window_seconds = bounded_int(
            window_seconds,
            86_400,
            60,
            365 * 24 * 60 * 60,
        )
        token_limit = bounded_int(
            token_limit,
            500_000,
            1,
            1_000_000_000,
        )
        session_id = ""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute(
                    """
                    SELECT id FROM sessions
                    WHERE platform_id = ? AND group_id = ?
                    ORDER BY selected DESC, updated_at DESC
                    LIMIT 1
                    """,
                    (platform_id, group_id),
                ).fetchone()
                if not session:
                    raise DatabaseNotFoundError("群会话不存在")
                session_id = str(session["id"])
                now = utc_now()
                connection.execute(
                    """
                    INSERT INTO token_quota_policies(
                        id, scope_type, scope_id, window_seconds,
                        token_limit, enabled, revision, updated_by, updated_at
                    ) VALUES (?, 'group', ?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(scope_type, scope_id) DO UPDATE SET
                        window_seconds = excluded.window_seconds,
                        token_limit = excluded.token_limit,
                        enabled = excluded.enabled,
                        revision = token_quota_policies.revision + 1,
                        updated_by = excluded.updated_by,
                        updated_at = excluded.updated_at
                    """,
                    (
                        new_id("quota"),
                        group_id,
                        window_seconds,
                        token_limit,
                        int(bool(enabled)),
                        actor_id,
                        now,
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "token.group_quota",
                    group_id,
                    {
                        "platform_id": platform_id,
                        "window_seconds": window_seconds,
                        "token_limit": token_limit,
                        "enabled": bool(enabled),
                    },
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._group_token_usage_summary(platform_id, group_id)

    async def ensure_default_token_quota(
        self,
        session_id: str,
        *,
        window_seconds: int,
        token_limit: int,
        enabled: bool,
        actor_id: str,
    ) -> dict[str, Any]:
        """为尚未配置任何配额策略的副本播种默认策略（v1.0-A2）。

        只在该副本（及其所属群）都不存在任何已启用策略时写入默认值；
        已有策略时保持原样返回，绝不覆盖运行时的显式配置。
        返回当前配额摘要（与 ``set_token_quota`` 一致）。
        """
        return await self._run(
            self._ensure_default_token_quota,
            session_id,
            int(window_seconds),
            int(token_limit),
            bool(enabled),
            actor_id,
        )

    def _ensure_default_token_quota(
        self,
        session_id: str,
        window_seconds: int,
        token_limit: int,
        enabled: bool,
        actor_id: str,
    ) -> dict[str, Any]:
        window_seconds = bounded_int(
            window_seconds,
            86_400,
            60,
            365 * 24 * 60 * 60,
        )
        token_limit = bounded_int(
            token_limit,
            500_000,
            1,
            1_000_000_000,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute(
                    "SELECT id, group_id FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not session:
                    raise DatabaseNotFoundError("副本不存在")
                existing = connection.execute(
                    """
                    SELECT 1 FROM token_quota_policies
                    WHERE enabled = 1 AND (
                        (scope_type = 'group' AND scope_id = ?)
                        OR (scope_type = 'session' AND scope_id = ?)
                    )
                    LIMIT 1
                    """,
                    (session["group_id"], session_id),
                ).fetchone()
                if existing:
                    connection.execute("COMMIT")
                    return self._token_usage_summary(session_id)
                if not enabled:
                    connection.execute("COMMIT")
                    return self._token_usage_summary(session_id)
                now = utc_now()
                connection.execute(
                    """
                    INSERT INTO token_quota_policies(
                        id, scope_type, scope_id, window_seconds,
                        token_limit, enabled, revision, updated_by, updated_at
                    ) VALUES (?, 'session', ?, ?, ?, 1, 1, ?, ?)
                    """,
                    (
                        new_id("quota"),
                        session_id,
                        window_seconds,
                        token_limit,
                        actor_id,
                        now,
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "token.quota_default",
                    session_id,
                    {
                        "window_seconds": window_seconds,
                        "token_limit": token_limit,
                    },
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._token_usage_summary(session_id)

    async def record_provider_result(
        self,
        provider_id: str,
        *,
        success: bool,
        reason: str = "",
        probe: bool = False,
        probe_status: str = "",
        probe_latency_ms: int = 0,
        probe_error_code: str = "",
        probe_expires_at: str = "",
        probe_idempotency_key: str = "",
    ) -> dict[str, Any]:
        return await self._run(
            self._record_provider_result,
            provider_id,
            success,
            reason,
            probe,
            probe_status,
            probe_latency_ms,
            probe_error_code,
            probe_expires_at,
            probe_idempotency_key,
        )
