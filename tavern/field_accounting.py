"""Host-independent actor field accounting and declaration validation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


class FieldAccountingError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        path: str = "actor.fields",
        expected: Any = None,
        actual: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.path = path
        self.expected = expected
        self.actual = actual

    def as_issue(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "path": self.path,
            "message": str(self),
            "expected": self.expected,
            "actual": self.actual,
        }


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def field_account(actor: Mapping[str, Any]) -> dict[str, Any]:
    fields = _sequence(actor.get("fields"))
    seen: set[str] = set()
    free_keys: list[str] = []
    by_stage = {"A": 0, "B": 0, "C": 0}
    for index, raw in enumerate(fields):
        if not isinstance(raw, Mapping):
            raise FieldAccountingError(
                "actor.field.invalid",
                f"角色字段第 {index + 1} 项不是对象",
                path=f"actor.fields[{index}]",
            )
        key = str(raw.get("key") or "").strip()
        if not key:
            raise FieldAccountingError(
                "actor.field.key_missing",
                f"角色字段第 {index + 1} 项缺少 key",
                path=f"actor.fields[{index}].key",
            )
        if key in seen:
            raise FieldAccountingError(
                "actor.field.duplicate_key",
                f"角色字段重复：{key}",
                path=f"actor.fields[{index}].key",
                actual=key,
            )
        seen.add(key)
        stage = str(raw.get("stage") or "A").strip().upper()
        if stage not in by_stage:
            raise FieldAccountingError(
                "actor.field.stage_invalid",
                f"角色字段 {key} 的阶段无效：{stage or '空'}",
                path=f"actor.fields[{index}].stage",
                expected=["A", "B", "C"],
                actual=stage,
            )
        by_stage[stage] += 1
        if str(raw.get("type") or "").strip() in {"text", "textarea"}:
            free_keys.append(key)

    flow = actor.get("creation_flow")
    flow = flow if isinstance(flow, Mapping) else {}
    packs = _sequence(flow.get("archetype_packs"))
    if not packs:
        packs = _sequence(actor.get("archetype_packs"))
    return {
        "fields": len(fields),
        "free_fields": len(free_keys),
        "free_field_keys": free_keys,
        "by_stage": by_stage,
        "modes": len(_sequence(flow.get("modes"))),
        "archetype_packs": len(packs),
    }


def validate_field_count_declarations(
    actor: Mapping[str, Any],
) -> list[dict[str, Any]]:
    try:
        account = field_account(actor)
    except FieldAccountingError as exc:
        return [exc.as_issue()]
    issues: list[dict[str, Any]] = []
    audit = actor.get("content_audit")
    audit = audit if isinstance(audit, Mapping) else {}
    if (
        "total_fields" in audit
        and int(audit.get("total_fields") or 0) != account["fields"]
    ):
        issues.append(
            {
                "code": "actor.field_count.content_audit_mismatch",
                "path": "actor.content_audit.total_fields",
                "message": "作者内容核算的字段总数与 fields 不一致",
                "expected": account["fields"],
                "actual": audit.get("total_fields"),
            }
        )
    declared = actor.get("creation_stages")
    declared = declared if isinstance(declared, Mapping) else {}
    if (
        "total_fields" in declared
        and int(declared.get("total_fields") or 0) != account["fields"]
    ):
        issues.append(
            {
                "code": "actor.field_count.total_mismatch",
                "path": "actor.creation_stages.total_fields",
                "message": "建卡阶段声明的字段总数与 fields 不一致",
                "expected": account["fields"],
                "actual": declared.get("total_fields"),
            }
        )
    stages = declared.get("stages")
    stages = stages if isinstance(stages, Mapping) else {}
    for stage, expected in account["by_stage"].items():
        stage_decl = stages.get(stage)
        stage_decl = stage_decl if isinstance(stage_decl, Mapping) else {}
        if (
            "field_count" in stage_decl
            and int(stage_decl.get("field_count") or 0) != expected
        ):
            issues.append(
                {
                    "code": "actor.field_count.stage_mismatch",
                    "path": (
                        f"actor.creation_stages.stages.{stage}.field_count"
                    ),
                    "message": (
                        f"建卡阶段 {stage} 的字段数量与 fields 不一致"
                    ),
                    "expected": expected,
                    "actual": stage_decl.get("field_count"),
                }
            )
    return issues
