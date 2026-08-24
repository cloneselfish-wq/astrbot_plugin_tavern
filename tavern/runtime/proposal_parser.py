"""D1-RUN-009 结构化 AI 提案解析（纯逻辑，无 I/O）。

模型只能输出结构化提案；本模块把模型原始文本规范化为
``ParsedProposal``，任何解析失败都返回结构化错误信封
（code/message/recovery/technical_refs），且绝不触碰数据库。

对应 D1-ARC-002 3.2 权威执行顺序的第三步。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..resolution import extract_json_object
from .finalization_service import canonical_json


PROPOSAL_SCHEMA = "tavern-ai-proposal/1.0.0-rc10"
MAX_COMMANDS = 8
MAX_TARGETS = 16
MAX_OPERATIONS = 128
MAX_RISKS = 8
MAX_NARRATIVE = 6000
MAX_PARAMETERS = 64

# 模型可提议的玩家确认事项；未知确认一律拒绝，防止绕过自主权门禁。
KNOWN_CONFIRMATIONS = frozenset({"contract", "death", "resource", "join"})


@dataclass(frozen=True)
class ParsedProposal:
    """规范化后的 AI 结构化提案（纯数据）。"""

    task_type: str
    command_ids: tuple[str, ...]
    targets: tuple[str, ...]
    parameters: dict[str, Any]
    narrative_draft: str
    known_risks: tuple[str, ...]
    operations: tuple[dict[str, Any], ...]
    confirmations: tuple[str, ...]
    payload_digest: str
    raw: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": PROPOSAL_SCHEMA,
            "task_type": self.task_type,
            "command_ids": list(self.command_ids),
            "targets": list(self.targets),
            "parameters": dict(self.parameters),
            "narrative_draft": self.narrative_draft,
            "known_risks": list(self.known_risks),
            "operations": [dict(item) for item in self.operations],
            "confirmations": list(self.confirmations),
            "payload_digest": self.payload_digest,
        }


@dataclass(frozen=True)
class ParseOutcome:
    """解析结果：成功携带提案，失败携带错误信封。"""

    ok: bool
    proposal: ParsedProposal | None
    error: dict[str, Any] | None


def _error(
    code: str,
    message: str,
    recovery: str = "",
    technical_refs: Sequence[str] = (),
) -> ParseOutcome:
    return ParseOutcome(
        ok=False,
        proposal=None,
        error={
            "code": code,
            "message": message,
            "recovery": recovery,
            "technical_refs": [str(item) for item in technical_refs],
        },
    )


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"提案字段 {field} 必须是非空文本")
    text = value.strip()
    if len(text) > maximum:
        raise ValueError(f"提案字段 {field} 超出长度上限 {maximum}")
    return text


def _string_list(
    value: Any,
    field: str,
    maximum: int,
    *,
    allow_empty: bool = False,
) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"提案字段 {field} 必须是数组")
    items = [str(item).strip() for item in value if str(item).strip()]
    if not items and not allow_empty:
        raise ValueError(f"提案字段 {field} 不能为空")
    if len(items) > maximum:
        raise ValueError(f"提案字段 {field} 超出数量上限 {maximum}")
    return items


def _operation_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("提案字段 operations 必须是数组")
    if len(value) > MAX_OPERATIONS:
        raise ValueError(f"提案字段 operations 超出数量上限 {MAX_OPERATIONS}")
    operations: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ValueError(f"操作 #{index + 1} 必须是对象")
        operation = dict(item)
        if not str(operation.get("op") or "").strip():
            raise ValueError(f"操作 #{index + 1} 缺少 op 类型")
        operations.append(operation)
    return operations


def parse_ai_proposal(raw_text: str) -> ParseOutcome:
    """解析模型原始输出为 ``ParsedProposal``。

    解析失败只返回错误信封，不产生任何写入。
    """

    try:
        payload = extract_json_object(raw_text)
    except ValueError as exc:
        return _error(
            "proposal.invalid_json",
            "AI 提案解析失败：模型未返回有效的结构化提案。",
            "系统未做任何改动。请重试；若持续失败，请向主持反馈该副本的 AI 提案格式。",
            [str(exc)],
        )
    if not isinstance(payload, Mapping):
        return _error(
            "proposal.invalid_json",
            "AI 提案解析失败：模型未返回对象。",
            "系统未做任何改动。请重试。",
        )
    body = dict(payload)
    schema = str(body.get("schema") or "").strip()
    if schema and schema != PROPOSAL_SCHEMA:
        return _error(
            "proposal.schema_mismatch",
            f"AI 提案使用了未知格式（{schema}）。",
            "系统未做任何改动。请重试；若持续失败，请向主持反馈该副本的 AI 提案格式。",
            [schema],
        )
    try:
        task_type = _text(body.get("task_type"), "task_type", 64)
        command_ids = _string_list(
            body.get("command_ids") or body.get("commands"),
            "command_ids",
            MAX_COMMANDS,
        )
        targets = _string_list(
            body.get("targets") or body.get("target_refs"),
            "targets",
            MAX_TARGETS,
            allow_empty=True,
        )
        parameters = _mapping(body.get("parameters"))
        if len(parameters) > MAX_PARAMETERS:
            raise ValueError(
                f"提案字段 parameters 超出数量上限 {MAX_PARAMETERS}"
            )
        narrative_draft = _text(
            body.get("narrative_draft") or body.get("narrative") or "",
            "narrative_draft",
            MAX_NARRATIVE,
        )
        known_risks = _string_list(
            body.get("known_risks") or body.get("risks"),
            "known_risks",
            MAX_RISKS,
            allow_empty=True,
        )
        operations = _operation_list(body.get("operations") or body.get("effects"))
        confirmations_raw = body.get("confirmations") or []
        if not isinstance(confirmations_raw, Sequence) or isinstance(
            confirmations_raw, (str, bytes)
        ):
            raise ValueError("提案字段 confirmations 必须是数组")
        confirmations = [
            str(item).strip().lower()
            for item in confirmations_raw
            if str(item).strip()
        ]
        unknown = [
            item for item in confirmations if item not in KNOWN_CONFIRMATIONS
        ]
        if unknown:
            return _error(
                "proposal.confirmation.unknown",
                "AI 提案包含系统不支持的确认事项。",
                "系统未做任何改动。请调整提案后重试。",
                [f"unknown confirmation: {item}" for item in unknown],
            )
    except ValueError as exc:
        return _error(
            "proposal.field.invalid",
            "AI 提案字段不合法：",
            "系统未做任何改动。请重试。",
            [str(exc)],
        )
    normalized = {
        "schema": PROPOSAL_SCHEMA,
        "task_type": task_type,
        "command_ids": command_ids,
        "targets": targets,
        "parameters": parameters,
        "narrative_draft": narrative_draft,
        "known_risks": known_risks,
        "operations": operations,
        "confirmations": confirmations,
    }
    proposal = ParsedProposal(
        task_type=task_type,
        command_ids=tuple(command_ids),
        targets=tuple(targets),
        parameters=parameters,
        narrative_draft=narrative_draft,
        known_risks=tuple(known_risks),
        operations=tuple(operations),
        confirmations=tuple(confirmations),
        payload_digest=canonical_json(normalized),
        raw=body,
    )
    return ParseOutcome(ok=True, proposal=proposal, error=None)


__all__ = [
    "KNOWN_CONFIRMATIONS",
    "MAX_COMMANDS",
    "MAX_NARRATIVE",
    "MAX_OPERATIONS",
    "MAX_PARAMETERS",
    "MAX_RISKS",
    "MAX_TARGETS",
    "ParseOutcome",
    "PROPOSAL_SCHEMA",
    "ParsedProposal",
    "parse_ai_proposal",
]
