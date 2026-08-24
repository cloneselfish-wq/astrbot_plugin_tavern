from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from ..condition_engine import ConditionEngine
from ..entity_registry import EntityRegistry
from ..operation_engine import (
    ACTOR_KINDS,
    build_operation_envelope,
)
from ..story_context import build_story_condition_context
from ..twp.commands import list_commands, preview_command
from .constants import TWP_COMMAND_SCHEMA
from .models import CommandEnvelope, CommandPlan
from .runtime import flatten_runtime, hydrate_runtime, runtime_from_state, store_runtime


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def normalize_envelope(
    value: Mapping[str, Any],
    *,
    operator: str = "",
    artifact_id: str = "",
) -> CommandEnvelope:
    targets = value.get("target_refs")
    if not isinstance(targets, Sequence) or isinstance(targets, (str, bytes)):
        targets = value.get("targets") or []
    payload = value.get("payload")
    payload = dict(payload) if isinstance(payload, Mapping) else {}
    for key, item in value.items():
        if key not in {
            "command_id",
            "api_version",
            "domain",
            "action",
            "actor_ref",
            "target_refs",
            "targets",
            "payload",
            "operator",
            "reason",
            "visibility",
            "idempotency_key",
            "operation_id",
            "expected_revision",
            "artifact_id",
        }:
            payload.setdefault(str(key), item)
    command_id = str(value.get("command_id") or f"cmd-{uuid.uuid4().hex}")
    operation = str(
        value.get("idempotency_key")
        or value.get("operation_id")
        or command_id
    )
    revision = value.get("expected_revision")
    return CommandEnvelope(
        command_id=command_id,
        api_version=str(value.get("api_version") or TWP_COMMAND_SCHEMA),
        domain=str(value.get("domain") or "").strip().lower(),
        action=str(value.get("action") or "").strip().lower(),
        actor_ref=str(value.get("actor_ref") or ""),
        target_refs=tuple(str(item) for item in targets),
        payload=payload,
        operator=str(value.get("operator") or operator),
        reason=str(value.get("reason") or ""),
        visibility=str(value.get("visibility") or "public"),
        idempotency_key=operation,
        expected_revision=(int(revision) if revision is not None else None),
        artifact_id=str(value.get("artifact_id") or artifact_id),
    )


def build_command_envelope(
    *,
    command_id: str,
    session_id: str,
    idempotency_key: str,
    actor: Mapping[str, Any],
    payload: Mapping[str, Any] | None = None,
    request_id: str = "",
    expected_revision: int | None = None,
    preview_only: bool = False,
) -> dict[str, Any]:
    """D1-RUN-003 统一命令信封。

    内部 ID 只允许出现在信封与技术审计中；玩家结果必须通过 Projection
    生成中文名称。与操作信封共用同一校验契约。
    """

    return build_operation_envelope(
        command_id=command_id,
        session_id=session_id,
        idempotency_key=idempotency_key,
        actor=actor,
        payload=payload,
        request_id=request_id,
        expected_revision=expected_revision,
        preview_only=preview_only,
    )


def validate_command_envelope(value: Any) -> dict[str, Any]:
    """Normalize and validate a D1 command envelope."""

    if not isinstance(value, Mapping):
        raise TypeError("命令信封必须是对象")
    actor_data = value.get("actor") or {}
    actor_data = actor_data if isinstance(actor_data, Mapping) else {}
    kind = str(actor_data.get("kind") or "").strip().lower()
    ref = str(actor_data.get("ref") or "").strip()
    if kind not in ACTOR_KINDS:
        raise ValueError(f"命令主体类型无效：{kind or '（空）'}")
    if not ref:
        raise ValueError("命令主体缺少稳定引用")
    return build_command_envelope(
        command_id=str(value.get("command_id") or ""),
        session_id=str(value.get("session_id") or ""),
        idempotency_key=str(value.get("idempotency_key") or ""),
        actor=actor_data,
        payload=value.get("payload") or {},
        request_id=str(value.get("request_id") or ""),
        expected_revision=value.get("expected_revision"),
        preview_only=bool(value.get("preview_only", False)),
    )


def _declared_command_conditions(
    world: Mapping[str, Any],
    domain: str,
    action: str,
) -> list[dict[str, Any]]:
    """从世界包声明的命令注册表读取该命令的条件树（D1-RUN-002）。"""

    rules = world.get("rules")
    rules = rules if isinstance(rules, Mapping) else {}
    registries: list[Any] = []
    scene_graph = rules.get("scene_graph")
    if isinstance(scene_graph, Mapping):
        registries.append(scene_graph.get("commands", []))
    for key in ("command_registry", "command_types"):
        value = rules.get(key)
        if isinstance(value, Mapping):
            value = value.get("commands", value.get("items", []))
        registries.append(value)
    conditions: list[dict[str, Any]] = []
    for raw_list in registries:
        if not isinstance(raw_list, Sequence) or isinstance(raw_list, (str, bytes)):
            continue
        for entry in raw_list:
            if not isinstance(entry, Mapping):
                continue
            matches = (
                str(entry.get("id") or "") == f"command:{domain}.{action}"
                or (
                    str(entry.get("domain") or "").lower() == str(domain).lower()
                    and str(entry.get("action") or "").lower() == str(action).lower()
                )
            )
            if not matches:
                continue
            declared = entry.get("conditions")
            if isinstance(declared, Sequence) and not isinstance(declared, (str, bytes)):
                conditions.extend(
                    dict(item) for item in declared if isinstance(item, Mapping)
                )
    return conditions


def evaluate_command_conditions(
    world: Mapping[str, Any],
    state: Mapping[str, Any],
    *,
    domain: str,
    action: str,
) -> list[dict[str, Any]]:
    """D1-RUN-004/014：命令前置条件使用注册运算符树求值。"""

    declared = _declared_command_conditions(world, domain, action)
    if not declared:
        return []
    engine = ConditionEngine(EntityRegistry(world))
    root = runtime_from_state(state)
    runtime = flatten_runtime(root)
    context = build_story_condition_context(
        world=world,
        runtime=runtime,
        session={"id": str(state.get("session_id") or "")},
    )
    results: list[dict[str, Any]] = []
    for condition in declared:
        try:
            evaluation = engine.evaluate_with_detail(condition, context)
            payload = evaluation.to_payload()
        except (KeyError, TypeError, ValueError) as exc:
            payload = {
                "allowed": False,
                "code": "condition.invalid",
                "message": str(exc),
                "recovery": "",
                "technical_refs": [],
            }
        payload["condition"] = condition
        results.append(payload)
    return results


def legacy_command(envelope: CommandEnvelope) -> dict[str, Any]:
    value = {
        "domain": envelope.domain,
        "action": envelope.action,
        "targets": list(envelope.target_refs),
        "operator": envelope.operator,
        "visibility": envelope.visibility,
        "idempotency_key": envelope.idempotency_key,
        "expected_revision": envelope.expected_revision,
        **dict(envelope.payload),
    }
    if envelope.reason:
        value["reason"] = envelope.reason
    return value


def command_catalog() -> list[dict[str, Any]]:
    result = []
    for item in list_commands():
        value = dict(item)
        value["api_version"] = TWP_COMMAND_SCHEMA
        value["idempotency_strategy"] = "required"
        value["preview_required"] = True
        value["target_types"] = list(value.get("target_types") or [])
        result.append(value)
    return result


def build_plan(
    world: Mapping[str, Any],
    state: Mapping[str, Any],
    envelope: CommandEnvelope | Mapping[str, Any],
) -> CommandPlan:
    env = (
        envelope
        if isinstance(envelope, CommandEnvelope)
        else normalize_envelope(envelope, artifact_id=str(world.get("artifact_id") or ""))
    )
    root = runtime_from_state(state)
    base_revision = int(root.get("revision") or 0)
    if env.expected_revision is not None and env.expected_revision != base_revision:
        raise ValueError(
            f"runtime.revision_conflict: expected={env.expected_revision}, current={base_revision}"
        )
    condition_results = evaluate_command_conditions(
        world,
        state,
        domain=env.domain,
        action=env.action,
    )
    blocked = [
        item for item in condition_results if not bool(item.get("allowed"))
    ]
    if blocked:
        first = blocked[0]
        raise ValueError(
            f"command.condition_failed:{first.get('code') or 'condition.not_matched'}"
            + (f" {first.get('message') or ''}" if first.get("message") else "")
        )
    current_state = dict(state)
    current_state["runtime"] = root
    report = preview_command(world, current_state, legacy_command(env))
    result = report.get("result") if isinstance(report, Mapping) else {}
    if not isinstance(result, Mapping):
        result = {}
    if not result and isinstance(report, Mapping):
        result = report
    result = dict(result)
    changes = result.get("changes") or []
    events = result.get("events") or []
    raw = {
        "envelope": env.export(),
        "base_revision": base_revision,
        "changes": changes,
        "events": events,
        "affected": report.get("affected") if isinstance(report, Mapping) else {},
        "conditions": condition_results,
    }
    plan_hash = hashlib.sha256(_canonical(raw)).hexdigest()
    return CommandPlan(
        plan_hash=plan_hash,
        base_revision=base_revision,
        revision_after=base_revision + 1,
        reads=tuple(
            {
                "scope": env.domain,
                "ref": target,
                "value_digest": hashlib.sha256(target.encode("utf-8")).hexdigest(),
            }
            for target in env.target_refs
        ),
        conditions=tuple(dict(item) for item in condition_results),
        changes=tuple(dict(item) for item in changes if isinstance(item, Mapping)),
        events=tuple(dict(item) for item in events if isinstance(item, Mapping)),
        visibility={"result": env.visibility},
        irreversible=env.domain == "ending" and env.action == "commit",
        requires_confirmation=env.domain == "ending" and env.action == "commit",
    )
