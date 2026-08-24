"""Domain repository methods extracted from the SQLite store."""

from ..database_support import *
from ..resolution_receipts import content_hash
from ..rule_runtime import RuleRuntime
from ..operation_state import (
    ACTIVE_OPERATION_STATUSES,
    ALL_OPERATION_STATUSES,
    CANCELLABLE_OPERATION_STATUSES,
    OperationStateMachine,
    TERMINAL_OPERATION_STATUSES,
)


OPERATION_GENERATION_STATUSES = frozenset(
    {
        "pending",
        "reserved",
        "generating",
        "dice_locked",
        "ready_to_commit",
    }
)
OPERATION_TERMINAL_STATUSES = TERMINAL_OPERATION_STATUSES
OPERATION_ALL_STATUSES = ALL_OPERATION_STATUSES
# 生成阶段操作默认租约时长；超时由 recover_expired_operations 接管，
# 避免回执永久停留在“处理中”。
OPERATION_LEASE_SECONDS = 600


def operation_revision(value: Mapping[str, Any]) -> int:
    """Return a JS-safe revision for one projected operation.

    ``operation_receipts`` predates row revisions.  The current runtime therefore compares a
    digest of the fields that decide whether cancellation is still safe.  The
    stable operation id is intentionally excluded from the public value.
    """

    payload = {
        "status": str(value.get("status") or ""),
        "phase": str(value.get("phase") or ""),
        "retry_count": int(value.get("retry_count") or 0),
        "lease_expires_at": str(value.get("lease_expires_at") or ""),
        "cancel_requested_at": str(value.get("cancel_requested_at") or ""),
        "updated_at": str(value.get("updated_at") or ""),
    }
    return int(content_hash(payload)[:13], 16)



__all__ = [name for name in globals() if not name.startswith('__')]
