"""D1-RUN-009 校验链（第 1-6 步）与 Effect 预览（第 7 步）。

顺序固定且短路：Schema -> 引用解析 -> 权限/会话状态 -> Condition ->
世界知识边界 -> 玩家自主权。校验失败不得进入 Effect 执行；
``preview`` 只做 dry-run，不产生任何写入。

所有外部依赖均为 Protocol（真实实现可注入 ``ConditionEngine`` /
``OperationEngine`` / ``EntityRegistry`` 等），保证本模块可独立测试。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..entity_registry import split_ref
from ..operation_engine import OPERATION_TYPES
from .proposal_parser import ParsedProposal


# 允许引用运行时实体的操作：这些操作的 target_ref 只校验引用形状，
# 不要求已注册（实体由叙事过程创建）。
RUNTIME_REF_OPS = frozenset(
    {"add_tag", "remove_tag", "create_instance", "emit_event"}
)
# 玩家名下实体前缀：AI 修改这些目标必须获得显式授权。
PLAYER_OWNED_PREFIXES = ("player:", "character:", "actor:")
# 模型不得触碰的领域：骰点回执与结算。
PROTECTED_REF_PREFIXES = ("receipt:", "dice:")


@runtime_checkable
class CommandRegistry(Protocol):
    """命令注册表：``get`` 返回 D1-RUN-002 命令定义或 None。"""

    def get(self, command_id: str) -> dict[str, Any] | None: ...


@runtime_checkable
class ConditionEvaluator(Protocol):
    """Condition 求值器：返回带 ``matched``/``reads`` 的结果对象。"""

    def evaluate(
        self,
        condition: Any,
        context: Mapping[str, Any],
    ) -> Any: ...


@runtime_checkable
class EntityRegistryLike(Protocol):
    """实体注册表：判断稳定引用是否已注册。"""

    def contains(self, ref: str) -> bool: ...


@runtime_checkable
class WorldCapabilitySource(Protocol):
    """世界包能力声明来源。"""

    def declared(self) -> set[str]: ...


@runtime_checkable
class OperationExecutor(Protocol):
    """Effect 计划执行器（与 ``OperationEngine`` 同形）。"""

    def validate(self, operations: Any) -> list[dict[str, Any]]: ...

    def apply(
        self,
        operations: Any,
        state: Mapping[str, Any] | None,
        *,
        dry_run: bool = False,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]: ...


@dataclass(frozen=True)
class ValidationResult:
    """校验结果：allowed=False 时携带首个失败检查的错误信封。"""

    allowed: bool
    code: str
    message: str
    recovery: str
    technical_refs: tuple[str, ...]
    checks: tuple[dict[str, Any], ...] = ()
    preview: dict[str, Any] | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "code": self.code,
            "message": self.message,
            "recovery": self.recovery,
            "technical_refs": list(self.technical_refs),
            "checks": [dict(item) for item in self.checks],
            "preview": dict(self.preview) if self.preview is not None else None,
        }


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def _string_set(value: Any) -> set[str]:
    return {str(item).strip() for item in _sequence(value) if str(item).strip()}


class ProposalValidator:
    """按 D1-RUN-009 顺序执行校验链与 Effect 预览（纯逻辑）。"""

    def __init__(
        self,
        *,
        commands: CommandRegistry,
        entities: EntityRegistryLike,
        conditions: ConditionEvaluator,
        capabilities: WorldCapabilitySource,
        executor: OperationExecutor,
    ) -> None:
        self.commands = commands
        self.entities = entities
        self.conditions = conditions
        self.capabilities = capabilities
        self.executor = executor

    def resolve_command(self, command_id: str) -> dict[str, Any] | None:
        """公开命令定义查询（供编排器构造 Event 使用）。"""

        return self.commands.get(command_id)

    def validate(
        self,
        proposal: ParsedProposal,
        request: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> ValidationResult:
        checks: list[dict[str, Any]] = []

        # 1. Schema：候选命令必须已注册。
        for command_id in proposal.command_ids:
            command = self.commands.get(command_id)
            if command is None:
                return self._fail(
                    checks,
                    "proposal.command.unregistered",
                    f"提案引用了未注册的命令（{command_id}）。",
                    "系统未做任何改动。请调整提案后重试。",
                    [command_id],
                )
            checks.append(
                {
                    "check": "schema",
                    "command_id": command_id,
                    "allowed": True,
                }
            )

        # 2. 引用解析：目标必须已注册。
        for target in proposal.targets:
            if not self.entities.contains(target):
                return self._fail(
                    checks,
                    "proposal.reference.unregistered",
                    f"提案引用了不存在的目标（{target}）。",
                    "系统未做任何改动。请调整提案后重试。",
                    [target],
                )
            checks.append({"check": "references", "target": target, "allowed": True})

        primary = self.commands.get(proposal.command_ids[0]) or {}

        # 3. 权限与会话状态。
        permission = str(primary.get("permission") or "").strip()
        allowed_permissions = _string_set(request.get("permissions"))
        if permission and permission not in allowed_permissions:
            return self._fail(
                checks,
                "proposal.permission.denied",
                "该操作需要更高的权限，当前身份无权执行。",
                "系统未做任何改动。请向主持申请权限，或改用本人可执行的命令。",
                [permission],
            )
        checks.append({"check": "permission", "permission": permission, "allowed": True})
        allowed_states = _string_set(primary.get("allowed_session_states"))
        session_state = str(request.get("session_state") or "").strip()
        if allowed_states and session_state not in allowed_states:
            return self._fail(
                checks,
                "proposal.session_state.invalid",
                f"当前副本状态不允许执行该操作（{session_state or '未知'}）。",
                "系统未做任何改动。请使用「/团 当前」查看副本状态后重试。",
                [session_state],
            )
        checks.append(
            {
                "check": "session_state",
                "session_state": session_state,
                "allowed": True,
            }
        )

        # 4. Condition：命令前置条件必须全部满足。
        for command_id in proposal.command_ids:
            command = self.commands.get(command_id) or {}
            for condition in _sequence(command.get("conditions")):
                result = self.conditions.evaluate(condition, context)
                if bool(getattr(result, "matched", False)):
                    continue
                payload = getattr(result, "to_payload", None)
                detail = _mapping(payload() if callable(payload) else None)
                return self._fail(
                    checks,
                    str(detail.get("code") or "condition.not_matched"),
                    str(detail.get("message") or "前置条件未满足，操作不可执行。"),
                    str(
                        detail.get("recovery")
                        or "系统未做任何改动。请满足前置条件后重试。"
                    ),
                    [str(item) for item in _sequence(detail.get("technical_refs"))],
                )
            checks.append(
                {
                    "check": "conditions",
                    "command_id": command_id,
                    "allowed": True,
                }
            )

        # 5. 世界知识边界：能力声明 + 操作目标 + 禁止触碰骰点回执/叙事绕过。
        declared_capabilities = self.capabilities.declared()
        for command_id in proposal.command_ids:
            command = self.commands.get(command_id) or {}
            required = _string_set(command.get("required_capabilities"))
            missing = required - declared_capabilities
            if missing:
                return self._fail(
                    checks,
                    "proposal.capability.undeclared",
                    "提案使用的能力未被当前世界包声明。",
                    "系统未做任何改动。请调整提案，只使用当前世界包允许的能力。",
                    sorted(missing),
                )
        checks.append({"check": "capabilities", "allowed": True})
        for index, operation in enumerate(proposal.operations):
            op = str(operation.get("op") or "").strip()
            if op not in OPERATION_TYPES:
                return self._fail(
                    checks,
                    "proposal.effect.unsupported_type",
                    f"提案包含不支持的效应类型（{op or '<empty>'}）。",
                    "系统未做任何改动。请调整提案后重试。",
                    [op],
                )
            target_ref = str(
                operation.get("target_ref") or operation.get("ref") or ""
            ).strip()
            if not target_ref:
                return self._fail(
                    checks,
                    "proposal.effect.missing_target",
                    f"效应 #{index + 1} 缺少作用目标。",
                    "系统未做任何改动。请调整提案后重试。",
                    [f"operation#{index + 1}"],
                )
            if target_ref.startswith(PROTECTED_REF_PREFIXES):
                return self._fail(
                    checks,
                    "proposal.effect.receipt_mutation",
                    "提案尝试修改骰点回执，已被拒绝。",
                    "系统未做任何改动。结算结果由系统统一记录，请勿在提案中修改。",
                    [target_ref],
                )
            if op in RUNTIME_REF_OPS:
                try:
                    split_ref(target_ref)
                except ValueError as exc:
                    return self._fail(
                        checks,
                        "proposal.effect.invalid_ref",
                        f"效应 #{index + 1} 的目标引用格式不合法。",
                        "系统未做任何改动。请调整提案后重试。",
                        [str(exc)],
                    )
            elif not self.entities.contains(target_ref):
                return self._fail(
                    checks,
                    "proposal.effect.unregistered_target",
                    f"效应引用了不存在的目标（{target_ref}）。",
                    "系统未做任何改动。请调整提案后重试。",
                    [target_ref],
                )
            if bool(operation.get("narrative_bypass")) or bool(
                operation.get("bypass_narration")
            ):
                return self._fail(
                    checks,
                    "proposal.effect.narrative_bypass",
                    "提案试图用叙事文字绕过资源消耗，已被拒绝。",
                    "系统未做任何改动。资源变化必须通过明确的效应声明执行。",
                    [f"operation#{index + 1}"],
                )
        checks.append({"check": "knowledge_boundary", "allowed": True})

        # 6. 玩家自主权：AI 不能替玩家确认死亡/签署外约，
        #    不能修改他人名下实体；确认事项必须显式授予。
        actor = _mapping(request.get("actor"))
        actor_kind = str(actor.get("kind") or "").strip().lower()
        confirmations = set(proposal.confirmations)
        if actor_kind == "ai":
            banned = confirmations & {"contract", "death"}
            if banned:
                return self._fail(
                    checks,
                    "proposal.autonomy.player_confirmation_required",
                    "AI 不能替玩家签署外约或确认死亡。",
                    "系统未做任何改动。请由玩家本人通过确认命令决定。",
                    sorted(banned),
                )
            allowed_targets = _string_set(request.get("allowed_targets"))
            for operation in proposal.operations:
                target_ref = str(
                    operation.get("target_ref") or operation.get("ref") or ""
                ).strip()
                if (
                    target_ref.startswith(PLAYER_OWNED_PREFIXES)
                    and target_ref not in allowed_targets
                ):
                    return self._fail(
                        checks,
                        "proposal.autonomy.actor_not_authorized",
                        "提案尝试修改未获授权的角色目标。",
                        "系统未做任何改动。请由目标角色本人确认，或由主持执行。",
                        [target_ref],
                    )
        granted = _string_set(request.get("confirmations_granted"))
        missing_confirmations = confirmations - granted
        if missing_confirmations:
            return self._fail(
                checks,
                "proposal.autonomy.confirmation_missing",
                "提案需要玩家确认后才会执行。",
                "系统未做任何改动。请先发送对应的确认命令，再重新发起操作。",
                sorted(missing_confirmations),
            )
        checks.append({"check": "autonomy", "allowed": True})

        return ValidationResult(
            allowed=True,
            code="proposal.validated",
            message="提案已通过全部校验。",
            recovery="",
            technical_refs=(),
            checks=tuple(checks),
        )

    def preview(
        self,
        proposal: ParsedProposal,
        context: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Effect 预览（第 7 步）：dry-run 计算状态变化，不产生写入。"""

        state, changes, narrative = self.executor.apply(
            proposal.operations,
            _mapping(context.get("state")),
            dry_run=True,
        )
        return {
            "operation_count": len(proposal.operations),
            "state_after": state,
            "changes": changes,
            "narrative": narrative,
            "payload_digest": proposal.payload_digest,
        }

    @staticmethod
    def _fail(
        checks: list[dict[str, Any]],
        code: str,
        message: str,
        recovery: str,
        technical_refs: Sequence[str] = (),
    ) -> ValidationResult:
        return ValidationResult(
            allowed=False,
            code=code,
            message=message,
            recovery=recovery,
            technical_refs=tuple(technical_refs),
            checks=tuple(checks),
        )


__all__ = [
    "CommandRegistry",
    "ConditionEvaluator",
    "EntityRegistryLike",
    "OperationExecutor",
    "PLAYER_OWNED_PREFIXES",
    "PROTECTED_REF_PREFIXES",
    "ProposalValidator",
    "RUNTIME_REF_OPS",
    "ValidationResult",
    "WorldCapabilitySource",
]
