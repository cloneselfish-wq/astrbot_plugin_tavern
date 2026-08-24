"""durable generation-operation state machine."""

from __future__ import annotations

from dataclasses import dataclass


ACTIVE_OPERATION_STATUSES = frozenset(
    {
        "pending",
        "reserved",
        "generating",
        "dice_locked",
        "ready_to_commit",
        "cancel_requested",
    }
)
CANCELLABLE_OPERATION_STATUSES = ACTIVE_OPERATION_STATUSES - {
    "cancel_requested"
}
TERMINAL_OPERATION_STATUSES = frozenset(
    {
        "completed",
        "failed",
        "failed_retryable",
        "needs_recovery",
        "compensated",
        "cancelled",
    }
)
ALL_OPERATION_STATUSES = ACTIVE_OPERATION_STATUSES | TERMINAL_OPERATION_STATUSES


_ALLOWED = {
    "pending": CANCELLABLE_OPERATION_STATUSES | TERMINAL_OPERATION_STATUSES,
    "reserved": frozenset(
        {
            "generating",
            "dice_locked",
            "ready_to_commit",
            "cancel_requested",
            "completed",
            "failed",
            "failed_retryable",
            "needs_recovery",
            "cancelled",
        }
    ),
    "generating": frozenset(
        {
            "dice_locked",
            "ready_to_commit",
            "cancel_requested",
            "completed",
            "failed",
            "failed_retryable",
            "needs_recovery",
            "cancelled",
        }
    ),
    "dice_locked": frozenset(
        {
            "generating",
            "ready_to_commit",
            "cancel_requested",
            "completed",
            "failed",
            "failed_retryable",
            "needs_recovery",
            "cancelled",
        }
    ),
    "ready_to_commit": frozenset(
        {
            "cancel_requested",
            "completed",
            "failed_retryable",
            "needs_recovery",
            "cancelled",
        }
    ),
    "cancel_requested": frozenset(
        {"cancel_requested", "cancelled", "completed", "needs_recovery"}
    ),
    "failed_retryable": frozenset(
        {"reserved", "cancelled", "needs_recovery"}
    ),
}


@dataclass(frozen=True, slots=True)
class OperationTransition:
    current: str
    target: str
    allowed: bool


class OperationStateMachine:
    """Single transition authority for durable generation operations."""

    @staticmethod
    def transition(current: str, target: str) -> OperationTransition:
        current = str(current or "pending").lower()
        target = str(target or "pending").lower()
        if current not in ALL_OPERATION_STATUSES:
            raise ValueError("现有操作状态无效")
        if target not in ALL_OPERATION_STATUSES:
            raise ValueError("目标操作状态无效")
        if current == target:
            return OperationTransition(current, target, True)
        if current in TERMINAL_OPERATION_STATUSES and current != "failed_retryable":
            return OperationTransition(current, target, False)
        return OperationTransition(
            current,
            target,
            target in _ALLOWED.get(current, frozenset()),
        )


__all__ = [
    "ACTIVE_OPERATION_STATUSES",
    "ALL_OPERATION_STATUSES",
    "CANCELLABLE_OPERATION_STATUSES",
    "OperationStateMachine",
    "OperationTransition",
    "TERMINAL_OPERATION_STATUSES",
]
