from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections.abc import Mapping, MutableMapping, Sequence
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from .entity_registry import EntityRegistry, split_ref


OPERATION_TYPES = frozenset(
    {
        "modify_value", "set_value", "grant_reference", "revoke_reference",
        "add_tag", "remove_tag", "create_instance", "end_instance",
        "set_visibility", "set_availability", "advance_counter",
        "modify_relationship", "emit_event", "request_resolution",
        "add_narrative_constraint",
    }
)
PERSISTENCE_SCOPES = frozenset(
    {"global_character", "world_character", "campaign", "session", "scene", "temporary"}
)
ACTOR_KINDS = frozenset({"player", "dm", "admin", "system", "ai"})
# D1-RUN-007: 统一 Receipt 状态机。
RECEIPT_STATUSES = frozenset(
    {
        "reserved",
        "validated",
        "committed",
        "delivery_pending",
        "delivered",
        "delivery_failed",
        "rejected",
        "rolled_back",
    }
)
ENVELOPE_SCHEMA = "tavern-operation-envelope/1.0.0-rc10"
ROLLBACK_SCHEMA = "tavern-rollback-plan/1.0.0-rc10"


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_operation_envelope(
    *,
    command_id: str,
    session_id: str,
    idempotency_key: str,
    actor: Mapping[str, Any],
    payload: Mapping[str, Any] | None = None,
    request_id: str = "",
    expected_revision: int | None = None,
    preview_only: bool = False,
    allow_empty_session: bool = False,
) -> dict[str, Any]:
    """D1-RUN-003 统一命令信封（纯数据契约）。

    内部 ID 只允许出现在信封与技术审计中；玩家结果必须通过 Projection
    生成中文名称。
    """

    command_id = str(command_id or "").strip()
    session_id = str(session_id or "").strip()
    idempotency_key = str(idempotency_key or "").strip()
    if not command_id:
        raise ValueError("操作信封缺少 command_id")
    if not session_id and not allow_empty_session:
        raise ValueError("操作信封缺少 session_id")
    if not idempotency_key:
        raise ValueError("操作信封缺少幂等键")
    actor_data = dict(actor) if isinstance(actor, Mapping) else {}
    kind = str(actor_data.get("kind") or "").strip().lower()
    ref = str(actor_data.get("ref") or "").strip()
    if kind not in ACTOR_KINDS:
        raise ValueError(f"操作主体类型无效：{kind or '（空）'}")
    if not ref:
        raise ValueError("操作主体缺少稳定引用")
    payload_data = dict(payload) if isinstance(payload, Mapping) else {}
    if expected_revision is not None:
        try:
            expected_revision = int(expected_revision)
        except (TypeError, ValueError):
            raise ValueError("expected_revision 必须是整数或 null")
        if expected_revision < 0:
            raise ValueError("expected_revision 不能为负数")
    envelope = {
        "schema": ENVELOPE_SCHEMA,
        "command_id": command_id[:160],
        "request_id": str(request_id or "").strip()[:160],
        "idempotency_key": idempotency_key[:240],
        "session_id": session_id[:160],
        "actor": {"kind": kind, "ref": ref[:160]},
        "payload": payload_data,
        "expected_revision": expected_revision,
        "preview_only": bool(preview_only),
    }
    envelope["envelope_hash"] = hashlib.sha256(_canonical(envelope)).hexdigest()
    return envelope


def validate_operation_envelope(value: Any) -> dict[str, Any]:
    """Normalize and validate an operation envelope from any mapping."""

    if not isinstance(value, Mapping):
        raise TypeError("操作信封必须是对象")
    return build_operation_envelope(
        command_id=str(value.get("command_id") or ""),
        session_id=str(value.get("session_id") or ""),
        idempotency_key=str(value.get("idempotency_key") or ""),
        actor=value.get("actor") or {},
        payload=value.get("payload") or {},
        request_id=str(value.get("request_id") or ""),
        expected_revision=value.get("expected_revision"),
        preview_only=bool(value.get("preview_only", False)),
    )


def build_operation_receipt(
    *,
    operation_id: str,
    idempotency_key: str = "",
    command_id: str = "",
    session_id: str = "",
    status: str = "committed",
    revision_before: int = 0,
    revision_after: int = 0,
    events: Sequence[Mapping[str, Any]] = (),
    changes_digest: str = "",
    projection_digest: str = "",
    error: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """D1-RUN-007 统一 Receipt 纯契约。

    状态、Event、Receipt 与 outbox 必须由仓储层同一事务提交；
    平台发送失败只更新投递状态，不重复执行领域 Effect。
    """

    status = str(status or "").strip().lower()
    if status not in RECEIPT_STATUSES:
        raise ValueError(f"Receipt 状态无效：{status or '（空）'}")
    operation_id = str(operation_id or "").strip()
    if not operation_id:
        raise ValueError("Receipt 缺少 operation_id")
    receipt = {
        "receipt_id": f"receipt_{uuid.uuid4().hex}",
        "schema": "tavern-operation-receipt/1.0.0-rc10",
        "operation_id": operation_id[:240],
        "idempotency_key": str(idempotency_key or "")[:240],
        "command_id": str(command_id or "")[:160],
        "session_id": str(session_id or "")[:160],
        "status": status,
        "revision_before": max(0, int(revision_before or 0)),
        "revision_after": max(0, int(revision_after or 0)),
        "events": [
            dict(item) for item in events if isinstance(item, Mapping)
        ],
        "changes_digest": str(changes_digest or ""),
        "projection_digest": str(projection_digest or ""),
        "error": dict(error) if isinstance(error, Mapping) else None,
        "created_at": utc_now(),
    }
    receipt["receipt_hash"] = hashlib.sha256(_canonical(receipt)).hexdigest()
    return receipt


def _numeric_inverse(item: Mapping[str, Any], change: Mapping[str, Any]) -> dict[str, Any]:
    strategy = str(
        change.get("aggregation_strategy")
        or item.get("aggregation_strategy")
        or "sum"
    )
    operand = change.get("operand")
    if operand is None:
        operand = float(item.get("value", item.get("delta", 0)) or 0)
    inverse: dict[str, Any] = {
        "op": "modify_value",
        "target_ref": str(change.get("target_ref") or item.get("target_ref") or ""),
        "persistence_scope": str(
            item.get("persistence_scope") or "session"
        ),
        "recipient": dict(item.get("recipient") or {})
        if isinstance(item.get("recipient"), Mapping)
        else {},
    }
    if strategy == "multiply":
        denominator = float(operand or 1) or 1
        inverse["aggregation_strategy"] = "multiply"
        inverse["value"] = 1.0 / denominator
    else:
        inverse["value"] = -float(operand or 0)
    return inverse


def build_rollback_plan(
    operations: Sequence[Mapping[str, Any]],
    changes: Sequence[Mapping[str, Any]],
    *,
    operation_id: str = "",
) -> dict[str, Any]:
    """Build a real inverse-operation plan for a prepared operation batch.

    Each entry is expressed in the same declarative operation format and can
    be fed back into ``OperationEngine.apply``.  Event-like operations have no
    state inverse and are listed as irreversible so the repository layer can
    compensate them through the receipt ledger instead.
    """

    batch = [dict(item) for item in operations if isinstance(item, Mapping)]
    change_rows = [dict(item) for item in changes if isinstance(item, Mapping)]
    entries: list[dict[str, Any]] = []
    irreversible: list[dict[str, Any]] = []
    for index, change in enumerate(change_rows):
        op = str(change.get("op") or "")
        ref = str(change.get("target_ref") or "")
        state_scope = str(change.get("state_scope") or "world")
        before = change.get("before")
        after = change.get("after")
        inverse: dict[str, Any] | None = None
        reversible = True
        if op in {"modify_value", "advance_counter"}:
            inverse = _numeric_inverse(batch[index] if index < len(batch) else {}, change)
            inverse["persistence_scope"] = str(
                (batch[index] if index < len(batch) else {}).get("persistence_scope")
                or "session"
            )
        elif op in {"set_value", "set_visibility", "set_availability"}:
            inverse = {
                "op": "set_value",
                "target_ref": ref,
                "value": deepcopy(before),
                "persistence_scope": str(
                    (batch[index] if index < len(batch) else {}).get("persistence_scope")
                    or "session"
                ),
            }
        elif op == "grant_reference":
            if before is True:
                reversible = False
            else:
                inverse = {
                    "op": "revoke_reference",
                    "target_ref": ref,
                    "persistence_scope": "session",
                }
        elif op == "revoke_reference":
            if before is False:
                reversible = False
            else:
                inverse = {
                    "op": "grant_reference",
                    "target_ref": ref,
                    "persistence_scope": "session",
                }
        elif op == "add_tag":
            if before is True:
                reversible = False
            else:
                inverse = {
                    "op": "remove_tag",
                    "target_ref": ref,
                    "value": str(change.get("tag_value") or ref),
                    "persistence_scope": "session",
                }
        elif op == "remove_tag":
            if before is False:
                reversible = False
            else:
                inverse = {
                    "op": "add_tag",
                    "target_ref": ref,
                    "value": str(change.get("tag_value") or ref),
                    "persistence_scope": "session",
                }
        elif op == "create_instance":
            if before is None:
                inverse = {
                    "op": "end_instance",
                    "target_ref": ref,
                    "instance_id": str(change.get("instance_id") or ref),
                    "persistence_scope": "session",
                }
            else:
                inverse = {
                    "op": "create_instance",
                    "target_ref": ref,
                    "instance_id": str(change.get("instance_id") or ref),
                    "value": deepcopy(before),
                    "persistence_scope": "session",
                }
        elif op == "end_instance":
            if before is None:
                reversible = False
            else:
                inverse = {
                    "op": "create_instance",
                    "target_ref": ref,
                    "instance_id": str(change.get("instance_id") or ref),
                    "value": deepcopy(before),
                    "persistence_scope": "session",
                }
        else:
            reversible = False
        if inverse is None:
            irreversible.append(
                {
                    "index": index,
                    "op": op,
                    "target_ref": ref,
                    "reason": "该操作没有可逆的状态逆操作",
                }
            )
            continue
        entries.append(
            {
                "index": index,
                "op": str(inverse["op"]),
                "target_ref": ref,
                "state_scope": state_scope,
                "operation": inverse,
                "reversible": reversible,
                "reason": f"撤销 #{index} 的 {op}",
            }
        )
    plan = {
        "schema": ROLLBACK_SCHEMA,
        "operation_id": str(operation_id or ""),
        "operation_count": len(batch),
        "entries": entries,
        "irreversible_ops": irreversible,
        "reversible": not irreversible and all(
            bool(item.get("reversible", True)) for item in entries
        ),
    }
    plan["plan_id"] = f"rollback:{hashlib.sha256(_canonical(plan)).hexdigest()[:24]}"
    return plan


class OperationEngine:
    MAX_OPERATIONS = 128

    def __init__(
        self,
        registry: EntityRegistry,
        numeric_policies: Mapping[str, Any] | None = None,
    ) -> None:
        self.registry = registry
        self.numeric_policies = dict(numeric_policies or {})

    def _numeric_policy(
        self, item: Mapping[str, Any], target_ref: str
    ) -> Mapping[str, Any] | None:
        requested = item.get("numeric_policy")
        if isinstance(requested, Mapping):
            return requested
        if isinstance(requested, str):
            candidate = self.numeric_policies.get(requested)
            if isinstance(candidate, Mapping):
                return candidate
        for key in (target_ref, target_ref.split(":", 1)[-1]):
            candidate = self.numeric_policies.get(key)
            if isinstance(candidate, Mapping):
                return candidate
        if self.registry.contains(target_ref):
            definition = self.registry.resolve(target_ref).definition
            candidate = definition.get("range")
            if isinstance(candidate, Mapping):
                return {**candidate, "overflow": candidate.get("overflow", "reject")}
        return None

    def validate(self, operations: Any) -> list[dict[str, Any]]:
        if not isinstance(operations, Sequence) or isinstance(operations, (str, bytes)):
            raise TypeError("操作批次必须是数组")
        if len(operations) > self.MAX_OPERATIONS:
            raise ValueError("单次操作数量超过技术安全上限")
        result: list[dict[str, Any]] = []
        for index, raw in enumerate(operations):
            if not isinstance(raw, Mapping):
                raise TypeError(f"操作 #{index + 1} 必须是对象")
            item = dict(raw)
            op = str(item.get("op") or "")
            if op not in OPERATION_TYPES:
                raise ValueError(f"不支持的操作：{op or '<empty>'}")
            scope = str(item.get("persistence_scope") or "session")
            if scope not in PERSISTENCE_SCOPES:
                raise ValueError(f"不支持的持久化作用域：{scope}")
            target_ref = str(item.get("target_ref") or item.get("ref") or "")
            if target_ref:
                split_ref(target_ref)
                runtime_ok = op in {"create_instance", "emit_event", "add_tag", "remove_tag"}
                if not runtime_ok and not self.registry.contains(target_ref):
                    raise ValueError(f"操作引用未注册：{target_ref}")
            value = item.get("value")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("操作数值不得为 NaN 或无穷值")
            result.append(item)
        return result

    def apply(
        self,
        operations: Any,
        state: Mapping[str, Any] | None,
        *,
        dry_run: bool = False,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
        batch = self.validate(operations)
        working: dict[str, Any] = deepcopy(dict(state or {}))
        scoped_state = any(
            key in working
            for key in ("world", "actor", "target", "scene", "session")
        )
        narrative: list[dict[str, Any]] = []
        changes: list[dict[str, Any]] = []

        for item in batch:
            op = str(item["op"])
            ref = str(item.get("target_ref") or item.get("ref") or "")
            recipient = item.get("recipient")
            recipient = recipient if isinstance(recipient, Mapping) else {}
            state_scope = str(
                recipient.get("scope") or item.get("state_scope") or "world"
            )
            bucket: MutableMapping[str, Any]
            if scoped_state:
                raw_bucket = working.setdefault(state_scope, {})
                if not isinstance(raw_bucket, MutableMapping):
                    raise TypeError(f"状态作用域不是对象：{state_scope}")
                bucket = raw_bucket
            else:
                bucket = working
            refs: MutableMapping[str, Any] = bucket.setdefault("refs", {})
            tags: list[str] = bucket.setdefault("tags", [])
            owned: list[str] = bucket.setdefault("references", [])
            instances: MutableMapping[str, Any] = bucket.setdefault("instances", {})
            before: Any = None
            after: Any = None
            if op in {"modify_value", "advance_counter", "modify_relationship"}:
                aggregate = str(item.get("aggregation_strategy") or "sum")
                default_before = 1 if aggregate == "multiply" else 0
                before = refs.get(ref, default_before)
                operand = float(item.get("value", item.get("delta", 0)) or 0)
                after = (
                    float(before if before is not None else default_before) * operand
                    if aggregate == "multiply"
                    else float(before or 0) + operand
                )
                policy = self._numeric_policy(item, ref)
                if isinstance(policy, Mapping):
                    minimum, maximum = policy.get("min"), policy.get("max")
                    if minimum is not None and after < float(minimum):
                        if policy.get("overflow", "reject") == "clamp": after = float(minimum)
                        else: raise ValueError(f"{ref} 低于世界规则下限")
                    if maximum is not None and after > float(maximum):
                        if policy.get("overflow", "reject") == "clamp": after = float(maximum)
                        else: raise ValueError(f"{ref} 超过世界规则上限")
                refs[ref] = int(after) if after.is_integer() else after
                after = refs[ref]
            elif op in {"set_value", "set_visibility", "set_availability"}:
                before, after = refs.get(ref), item.get("value")
                refs[ref] = after
            elif op == "grant_reference":
                before = ref in owned
                if ref not in owned: owned.append(ref)
                after = True
            elif op == "revoke_reference":
                before = ref in owned
                owned[:] = [value for value in owned if value != ref]
                after = False
            elif op == "add_tag":
                value = str(item.get("value") or ref)
                before = value in tags
                if value not in tags: tags.append(value)
                after = True
            elif op == "remove_tag":
                value = str(item.get("value") or ref)
                before = value in tags
                tags[:] = [tag for tag in tags if tag != value]
                after = False
            elif op == "create_instance":
                instance_id = str(item.get("instance_id") or ref)
                before = instances.get(instance_id)
                if before is not None and item.get("grant_policy", "ignore") == "ignore":
                    after = before
                else:
                    after = deepcopy(item.get("value") or item.get("definition") or {})
                    instances[instance_id] = after
            elif op == "end_instance":
                instance_id = str(item.get("instance_id") or ref)
                before = instances.pop(instance_id, None)
                after = None
            elif op == "add_narrative_constraint":
                projection = {
                    "source_ref": str(item.get("source_ref") or ""),
                    "text": str(item.get("value") or item.get("text") or ""),
                    "visibility": str(item.get("visibility") or "public"),
                }
                narrative.append(projection)
                after = projection
            elif op in {"emit_event", "request_resolution"}:
                after = deepcopy(item.get("value") or item)
                bucket.setdefault("emitted", []).append(after)
            change_record: dict[str, Any] = {
                "op": op,
                "state_scope": state_scope,
                "target_ref": ref,
                "before": before,
                "after": after,
            }
            if op in {"modify_value", "advance_counter"}:
                change_record["aggregation_strategy"] = str(
                    item.get("aggregation_strategy") or "sum"
                )
                change_record["operand"] = float(
                    item.get("value", item.get("delta", 0)) or 0
                )
            if op in {"add_tag", "remove_tag"}:
                change_record["tag_value"] = str(item.get("value") or ref)
            if op in {"create_instance", "end_instance"}:
                change_record["instance_id"] = str(
                    item.get("instance_id") or ref
                )
            changes.append(change_record)

        return (dict(state or {}) if dry_run else working), changes, narrative


__all__ = [
    "ACTOR_KINDS",
    "ENVELOPE_SCHEMA",
    "OPERATION_TYPES",
    "OperationEngine",
    "PERSISTENCE_SCOPES",
    "RECEIPT_STATUSES",
    "ROLLBACK_SCHEMA",
    "build_operation_envelope",
    "build_operation_receipt",
    "build_rollback_plan",
    "validate_operation_envelope",
]
