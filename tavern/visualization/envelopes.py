"""VisualEnvelope and safe component-level problem projection."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence


VISUAL_SCHEMA = "tavern-visual-envelope/1.0.0"
_WIRE_SCHEMA_KEYS = frozenset({"schema", "schema_version"})
_PUBLIC_REVISION_KEYS = frozenset(
    {"world_revision", "manifest_revision", "profile_revision"}
)
SERVER_STATES = frozenset(
    {
        "ready",
        "empty",
        "error",
        "stale",
        "partial",
        "readonly",
        "permission",
        "conflict",
        "unsupported",
    }
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _is_generic_wire_metadata(key: object) -> bool:
    name = str(key).strip().casefold()
    if name in _WIRE_SCHEMA_KEYS or name.endswith("_schema"):
        return True
    if name not in _PUBLIC_REVISION_KEYS and (
        name == "revision" or name.endswith("_revision")
    ):
        return True
    return False


def _is_action_descriptor(value: Mapping[str, Any]) -> bool:
    expected = value.get("expected_revision")
    return (
        isinstance(value.get("action_id"), str)
        and bool(str(value.get("action_id") or "").strip())
        and isinstance(value.get("intent"), str)
        and bool(str(value.get("intent") or "").strip())
        and isinstance(value.get("target_kind"), str)
        and bool(str(value.get("target_kind") or "").strip())
        and value.get("transportReady") is True
        and (
            (type(expected) is int and expected >= 0)
            or (isinstance(expected, str) and bool(expected.strip()))
        )
    )


def _public_wire_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        descriptor = _is_action_descriptor(value)
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if _is_generic_wire_metadata(key):
                if not (descriptor and key == "expected_revision"):
                    continue
            result[key] = _public_wire_value(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_public_wire_value(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class VisualProblem:
    code: str
    message: str
    recovery: str
    retryable: bool = False
    retry_after_seconds: int | None = None
    preserved_revision: int | str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": str(self.code or "visual.error"),
            "message": str(self.message or "数据暂时不可用。"),
            "recovery": str(self.recovery or "请稍后重试当前板块。"),
            "retryable": bool(self.retryable),
        }
        if self.retry_after_seconds is not None:
            result["retry_after_seconds"] = max(
                0, int(self.retry_after_seconds)
            )
        return result


@dataclass(frozen=True, slots=True)
class VisualEnvelope:
    kind: str
    state: str
    revision: int | str | None
    updated_at: str
    stale: bool
    summary: Mapping[str, Any] = field(default_factory=dict)
    data: Any = field(default_factory=dict)
    problems: Sequence[VisualProblem | Mapping[str, Any]] = field(
        default_factory=tuple
    )
    permissions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.state not in SERVER_STATES:
            raise ValueError(f"非法服务端视觉状态：{self.state}")
        if self.state == "stale" and not self.stale:
            raise ValueError("stale 状态必须同时设置 stale=true")
        if self.state == "permission" and bool(
            _mapping(self.permissions).get("can_view", True)
        ):
            raise ValueError("permission 状态不能声明 can_view=true")

    def to_dict(self) -> dict[str, Any]:
        serialized_problems: list[dict[str, Any]] = []
        for problem in self.problems:
            if isinstance(problem, VisualProblem):
                serialized_problems.append(problem.to_dict())
            elif isinstance(problem, Mapping):
                serialized_problems.append(_public_wire_value(problem))
        permissions = {
            str(key): bool(value)
            for key, value in _mapping(self.permissions).items()
            if str(key).startswith("can_")
        }
        permissions.setdefault("can_view", self.state != "permission")
        permissions.setdefault("can_manage", False)
        return {
            "kind": str(self.kind),
            "state": self.state,
            "revision": self.revision,
            "updated_at": str(self.updated_at or ""),
            "stale": bool(self.stale),
            "summary": _public_wire_value(self.summary),
            "data": _public_wire_value(self.data),
            "problems": serialized_problems,
            "permissions": permissions,
        }


def visual_envelope(
    *,
    kind: str,
    data: Any,
    revision: int | str | None,
    updated_at: str = "",
    summary: Mapping[str, Any] | None = None,
    permissions: Mapping[str, Any] | None = None,
    problems: Sequence[VisualProblem | Mapping[str, Any]] | None = None,
    state: str | None = None,
    empty: bool = False,
    stale: bool = False,
    readonly: bool = False,
) -> VisualEnvelope:
    problem_rows = tuple(problems or ())
    permission_rows = _mapping(permissions)
    can_view = bool(permission_rows.get("can_view", True))
    if state is None:
        if not can_view:
            state = "permission"
        elif readonly:
            state = "readonly"
        elif stale:
            state = "stale"
        elif problem_rows and data not in ({}, [], None):
            state = "partial"
        elif problem_rows:
            state = "error"
        elif empty:
            state = "empty"
        else:
            state = "ready"
    return VisualEnvelope(
        kind=str(kind),
        state=state,
        revision=revision,
        updated_at=str(updated_at or utc_now()),
        stale=bool(stale or state == "stale"),
        summary=_mapping(summary),
        data=data,
        problems=problem_rows,
        permissions=permission_rows,
    )


def problem_from_exception(
    exc: BaseException,
    *,
    preserved_revision: int | str | None = None,
) -> tuple[int, VisualProblem]:
    """Map an exception through the existing Web boundary without trace data."""

    from ..web.errors import build_envelope

    envelope = build_envelope(exc, include_technical=False)
    retry_after = getattr(exc, "retry_after_seconds", None)
    return int(envelope.status_code), VisualProblem(
        code=str(envelope.code),
        message=str(envelope.message),
        recovery=str(envelope.recovery),
        retryable=int(envelope.status_code) in {409, 429, 500, 502, 503, 504},
        retry_after_seconds=(
            int(retry_after) if retry_after not in (None, "") else None
        ),
        preserved_revision=preserved_revision,
    )


__all__ = [
    "SERVER_STATES",
    "VISUAL_SCHEMA",
    "VisualEnvelope",
    "VisualProblem",
    "problem_from_exception",
    "utc_now",
    "visual_envelope",
]
