from __future__ import annotations

from .rules_support import *


class RuleRuntimeQueriesRepositoryMixin:
    def _lock_check_result(
        self,
        operation_id: str,
        session_id: str,
        request_payload: dict[str, Any],
        result_payload: dict[str, Any],
    ) -> dict[str, Any]:
        operation_id = clean_text(operation_id, max_chars=240)
        if not operation_id or not session_id:
            raise ValueError("检定操作 ID 与副本 ID 不能为空")
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                existing = connection.execute(
                    """
                    SELECT * FROM operation_receipts
                    WHERE operation_id = ?
                    """,
                    (operation_id,),
                ).fetchone()
                if existing:
                    if (
                        existing["session_id"] != session_id
                        or existing["operation_type"] != "dice_check"
                    ):
                        raise DatabaseConflictError("检定操作 ID 已被其他请求使用")
                    connection.execute("COMMIT")
                    return {
                        "operation_id": existing["operation_id"],
                        "session_id": existing["session_id"],
                        "operation_type": existing["operation_type"],
                        "request": json_load(existing["request_json"], {}),
                        "result": json_load(existing["result_json"], {}),
                        "status": existing["status"],
                        "created_at": existing["created_at"],
                        "updated_at": existing["updated_at"],
                    }
                connection.execute(
                    """
                    INSERT INTO operation_receipts(
                        operation_id, session_id, operation_type,
                        request_json, result_json, status, created_at,
                        updated_at
                    ) VALUES (?, ?, 'dice_check', ?, ?, 'completed', ?, ?)
                    """,
                    (
                        operation_id,
                        session_id,
                        json_dump(request_payload),
                        json_dump(result_payload),
                        now,
                        now,
                    ),
                )
                connection.execute("COMMIT")
                return {
                    "operation_id": operation_id,
                    "session_id": session_id,
                    "operation_type": "dice_check",
                    "request": request_payload,
                    "result": result_payload,
                    "status": "completed",
                    "created_at": now,
                    "updated_at": now,
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise
