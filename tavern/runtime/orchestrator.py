"""D1-ARC-002 权威执行顺序编排器（骨架）。

执行顺序（D1-ARC-002 3.2 / D1-RUN-009）：

    Request -> 任务分类 -> 模型路由 -> 结构化提案 -> Schema/引用/权限/
    Condition/知识边界/自主权校验 -> Effect 预览 -> 事务提交 ->
    Event/Receipt/Outbox（同事务）-> 终局核算（同事务）-> 外显 Projection。

不变量：
- 模型输出绝不直接写库：模型原始文本只进入解析器，解析结果只以
  ``ParsedProposal``（纯 dict）形式经注入的事务协议落库；
- 校验失败不会进入 Effect 执行，也不会开启事务；
- Event/Receipt/Outbox 必须同事务提交（D1-ARC-004）；
- 平台投递失败不回滚已提交的领域事务：本模块只把 outbox 记录写进
  事务，实际发送由独立 worker 在事务外执行（D1-ARC-002 3.4）。

所有 I/O 依赖均为 Protocol，可用假实现独立测试；
终局核算默认复用 ``terminal_service`` / ``finalization_service`` 的
纯函数（可注入替换）。
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ..operation_engine import (
    build_operation_envelope,
    build_operation_receipt,
)
from ..performance import RequestProfiler
from .finalization_service import (
    build_finalization_plan,
    classify_plan,
    world_terminal_conditions,
)
from .model_router import ModelRoute, ModelRouter, classify_request
from .proposal_parser import ParsedProposal, parse_ai_proposal
from .proposal_validator import (
    OperationExecutor,
    ProposalValidator,
)
from .terminal_service import (
    arbitrate_terminal_conditions,
    build_terminal_context,
    evaluate_terminal_conditions,
)
from .contracts import CommandError, CommandResult
from .request import RequestContext


_LOGGER = logging.getLogger(__name__)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


@runtime_checkable
class ModelClient(Protocol):
    """模型客户端：输入路由与请求，返回原始文本（绝不直接写库）。"""

    async def generate(
        self,
        route: ModelRoute,
        request: Mapping[str, Any],
    ) -> str: ...


@runtime_checkable
class Transaction(Protocol):
    """领域事务边界：Event/Receipt/Outbox/终局必须在同一事务内提交。"""

    async def find_receipt(self, idempotency_key: str) -> dict[str, Any] | None: ...

    async def current_revision(self, session_id: str) -> int: ...

    async def apply_state(
        self,
        *,
        session_id: str,
        expected_revision: int | None,
        state: Mapping[str, Any],
        changes: Sequence[Mapping[str, Any]],
    ) -> int: ...

    async def append_event(self, event: Mapping[str, Any]) -> dict[str, Any]: ...

    async def write_receipt(self, receipt: Mapping[str, Any]) -> dict[str, Any]: ...

    async def enqueue_outbox(
        self,
        record: Mapping[str, Any],
    ) -> dict[str, Any]: ...

    async def apply_terminal_plan(
        self,
        plan: Mapping[str, Any],
    ) -> dict[str, Any]: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


@runtime_checkable
class TransactionFactory(Protocol):
    """开启领域事务的工厂。"""

    async def begin(self, request: Mapping[str, Any]) -> Transaction: ...


@runtime_checkable
class ProjectionService(Protocol):
    """外显投影：事务提交后把结果转换为玩家/主持可见视图。"""

    async def project(
        self,
        *,
        request: Mapping[str, Any],
        outcome: Mapping[str, Any],
    ) -> dict[str, Any]: ...


@runtime_checkable
class TerminalEvaluator(Protocol):
    """终局核算（D1-RUN-012/013）：求值 -> 建计划 -> 并发判定。"""

    def evaluate(self, context: Mapping[str, Any]) -> dict[str, Any]: ...

    def build_plan(
        self,
        *,
        session_id: str,
        match: Mapping[str, Any],
        trigger_revision: int,
    ) -> dict[str, Any]: ...

    def classify(
        self,
        plan: Mapping[str, Any],
        receipts: Sequence[Mapping[str, Any]],
    ) -> str: ...


@runtime_checkable
class OutboxPlanner(Protocol):
    """生成待投递记录（由投递服务预构造，编排器只负责同事务写入）。"""

    def plan(
        self,
        *,
        proposal: ParsedProposal,
        request: Mapping[str, Any],
        changes: Sequence[Mapping[str, Any]],
        narrative: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]: ...


class _DefaultOutboxPlanner:
    """默认取请求方（投递服务）预构造的 outbox 记录；无记录则为空。"""

    def plan(
        self,
        *,
        proposal: ParsedProposal,
        request: Mapping[str, Any],
        changes: Sequence[Mapping[str, Any]],
        narrative: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        records = _sequence(request.get("outbox_records"))
        return [dict(item) for item in records if isinstance(item, Mapping)]


class _DefaultTerminalEvaluator:
    """复用 terminal_service / finalization_service 的纯函数默认实现。"""

    def evaluate(self, context: Mapping[str, Any]) -> dict[str, Any]:
        world = _mapping(context.get("world"))
        conditions = _sequence(context.get("terminal_conditions"))
        if not conditions:
            conditions = world_terminal_conditions(world)
        terminal_context = build_terminal_context(
            world=world,
            session=_mapping(context.get("session")),
            party=_mapping(context.get("party")),
            capabilities=_sequence(context.get("capabilities")),
        )
        matches = evaluate_terminal_conditions(conditions, terminal_context)
        winner = arbitrate_terminal_conditions(matches)
        return {
            "matched": winner is not None,
            "winner": winner,
            "matches": [
                item.to_dict()
                if hasattr(item, "to_dict")
                else dict(item)
                for item in matches
            ],
        }

    def build_plan(
        self,
        *,
        session_id: str,
        match: Mapping[str, Any],
        trigger_revision: int,
    ) -> dict[str, Any]:
        return build_finalization_plan(
            session_id=session_id,
            match=match,
            trigger_revision=trigger_revision,
        )

    def classify(
        self,
        plan: Mapping[str, Any],
        receipts: Sequence[Mapping[str, Any]],
    ) -> str:
        return classify_plan(plan, receipts)


@dataclass(frozen=True)
class OrchestrationResult:
    """一次编排的完整结果（含玩家可见文案与内部技术详情分离）。"""

    status: str
    stage: str
    code: str
    message: str
    recovery: str
    technical_refs: tuple[str, ...] = ()
    category: str = ""
    route: dict[str, Any] | None = None
    proposal: dict[str, Any] | None = None
    preview: dict[str, Any] | None = None
    receipt: dict[str, Any] | None = None
    event_ids: tuple[str, ...] = ()
    outbox_ids: tuple[str, ...] = ()
    terminal_plan: dict[str, Any] | None = None
    projection: dict[str, Any] | None = None
    error: dict[str, Any] | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "stage": self.stage,
            "code": self.code,
            "message": self.message,
            "recovery": self.recovery,
        }
        if self.technical_refs:
            payload["technical_refs"] = list(self.technical_refs)
        for key in (
            "proposal",
            "preview",
            "receipt",
            "projection",
        ):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        if self.event_ids:
            payload["event_ids"] = list(self.event_ids)
        if self.outbox_ids:
            payload["outbox_ids"] = list(self.outbox_ids)
        if self.terminal_plan is not None:
            payload["terminal_plan"] = self.terminal_plan
        return payload


class Orchestrator:
    """权威执行顺序编排器（骨架，全部 I/O 走注入协议）。"""

    def __init__(
        self,
        *,
        validator: ProposalValidator,
        model: ModelClient,
        factory: TransactionFactory,
        projections: ProjectionService,
        router: ModelRouter | None = None,
        parser: Any = None,
        terminal: TerminalEvaluator | None = None,
        outbox_planner: OutboxPlanner | None = None,
        executor: OperationExecutor | None = None,
        now_fn: Any = None,
    ) -> None:
        self.router = router or ModelRouter()
        self.parser = parser or parse_ai_proposal
        self.validator = validator
        self.executor = executor
        self.model = model
        self.factory = factory
        self.projections = projections
        self.terminal = terminal or _DefaultTerminalEvaluator()
        self.outbox_planner = outbox_planner or _DefaultOutboxPlanner()
        self.now_fn = now_fn or (lambda: "")

    async def run(
        self,
        request: Mapping[str, Any],
        *,
        preview_only: bool = False,
    ) -> OrchestrationResult:
        """执行一次完整编排。"""

        request = dict(request or {})
        context = _mapping(request.get("context"))
        try:
            category = classify_request(request)
            route = self.router.route(category, request)
        except ValueError as exc:
            return self._reject(
                stage="classify",
                code="orchestration.category.invalid",
                message="请求无法分类或缺少对应模型路由。",
                recovery="系统未做任何改动。请检查请求参数后重试。",
                technical_refs=(str(exc),),
            )

        raw = await self.model.generate(route, request)
        parsed = self.parser(raw)
        if not parsed.ok:
            error = _mapping(parsed.error)
            return self._reject(
                stage="parse",
                code=str(error.get("code") or "proposal.invalid"),
                message=str(error.get("message") or "AI 提案解析失败。"),
                recovery=str(
                    error.get("recovery")
                    or "系统未做任何改动。请重试；若持续失败，请向主持反馈。"
                ),
                technical_refs=[
                    str(item) for item in _sequence(error.get("technical_refs"))
                ],
                category=category,
                route=route.to_dict(),
            )
        proposal = parsed.proposal
        if proposal is None:
            return self._reject(
                stage="parse",
                code="proposal.missing",
                message="AI 提案解析失败。",
                recovery="系统未做任何改动。请重试。",
                category=category,
                route=route.to_dict(),
            )

        validation = self.validator.validate(proposal, request, context)
        if not validation.allowed:
            return OrchestrationResult(
                status="rejected",
                stage="validate",
                code=validation.code,
                message=validation.message,
                recovery=validation.recovery,
                technical_refs=validation.technical_refs,
                category=category,
                route=route.to_dict(),
                proposal=proposal.to_dict(),
            )

        preview = self.validator.preview(proposal, context)
        if preview_only:
            return OrchestrationResult(
                status="preview",
                stage="preview",
                code="preview.ready",
                message="Effect 预览已生成，尚未提交任何改动。",
                recovery="确认无误后，请再次发送该操作以执行。",
                category=category,
                route=route.to_dict(),
                proposal=proposal.to_dict(),
                preview=preview,
            )

        tx = await self.factory.begin(request)
        exec_stage = "execute"
        try:
            idempotency_key = self._idempotency_key(request, proposal)
            existing = await tx.find_receipt(idempotency_key)
            if existing is not None:
                existing_status = str(existing.get("status") or "")
                if existing_status in {"reserved", "validated"}:
                    await self._safe_rollback(tx)
                    return OrchestrationResult(
                        status="processing",
                        stage="execute",
                        code="idempotency.in_flight",
                        message="该操作正在执行中，请稍后查询结果。",
                        recovery="请稍后重试，或使用「/团 当前」查看副本状态。",
                        category=category,
                        route=route.to_dict(),
                        proposal=proposal.to_dict(),
                    )
                stored_digest = str(
                    existing.get("payload_digest")
                    or existing.get("changes_digest")
                    or ""
                )
                if stored_digest != proposal.payload_digest:
                    await self._safe_rollback(tx)
                    return OrchestrationResult(
                        status="conflict",
                        stage="execute",
                        code="idempotency.payload_conflict",
                        message="相同的操作请求已提交过不同内容，本次未执行。",
                        recovery="如需再次尝试，请使用新的操作请求。",
                        category=category,
                        route=route.to_dict(),
                        proposal=proposal.to_dict(),
                    )
                await self._safe_rollback(tx)
                return OrchestrationResult(
                    status="replayed",
                    stage="execute",
                    code="idempotency.replayed",
                    message="该操作此前已成功执行，系统直接返回原结果，未重复修改。",
                    recovery="无需再次操作。",
                    category=category,
                    route=route.to_dict(),
                    proposal=proposal.to_dict(),
                    receipt=dict(existing),
                )

            session_id = str(request.get("session_id") or "").strip()
            actor = _mapping(request.get("actor"))
            command = self.validator.resolve_command(proposal.command_ids[0]) or {}
            envelope = build_operation_envelope(
                command_id=proposal.command_ids[0],
                session_id=session_id,
                idempotency_key=idempotency_key,
                actor=actor,
                payload=proposal.parameters,
                request_id=str(request.get("request_id") or ""),
                expected_revision=request.get("expected_revision"),
                preview_only=False,
            )
            executor = self.executor or self.validator.executor
            state, changes, narrative = executor.apply(
                proposal.operations,
                _mapping(context.get("state")),
                dry_run=False,
            )
            revision_before = await tx.current_revision(session_id)
            revision_after = await tx.apply_state(
                session_id=session_id,
                expected_revision=envelope.get("expected_revision"),
                state=state,
                changes=changes,
            )
            exec_stage = "commit"

            event = self._build_event(envelope, proposal, command, changes)
            event_row = await tx.append_event(event)
            receipt = build_operation_receipt(
                operation_id=str(envelope["envelope_hash"]),
                idempotency_key=idempotency_key,
                command_id=envelope["command_id"],
                session_id=session_id,
                status="committed",
                revision_before=revision_before,
                revision_after=revision_after,
                events=[dict(event_row or event)],
                changes_digest=proposal.payload_digest,
            )
            receipt_row = await tx.write_receipt(receipt)

            outbox_ids: list[str] = []
            for record in self.outbox_planner.plan(
                proposal=proposal,
                request=request,
                changes=changes,
                narrative=narrative,
            ):
                row = await tx.enqueue_outbox(record)
                outbox_ids.append(
                    str(
                        (row or record).get("delivery_id")
                        or record.get("delivery_id")
                        or ""
                    )
                )

            terminal_plan: dict[str, Any] | None = None
            verdict = self.terminal.evaluate(
                self._terminal_context(request, state)
            )
            winner = _mapping(verdict.get("winner"))
            if bool(verdict.get("matched")) and winner:
                plan = self.terminal.build_plan(
                    session_id=session_id,
                    match=winner,
                    trigger_revision=revision_after,
                )
                decision = self.terminal.classify(
                    plan,
                    [dict(receipt_row or receipt)],
                )
                if decision == "apply":
                    await tx.apply_terminal_plan(plan)
                    terminal_plan = dict(plan)

            await tx.commit()
        except Exception as exc:  # noqa: BLE001 - 统一回滚并返回错误信封
            await self._safe_rollback(tx)
            return OrchestrationResult(
                status="failed",
                stage=exec_stage,
                code="orchestration.transaction.failed",
                message="操作执行失败，系统已回滚，未留下部分改动。",
                recovery="请稍后重试；若持续失败，请联系主持查看技术详情。",
                technical_refs=(f"{type(exc).__name__}: {exc}",),
                category=category,
                route=route.to_dict(),
                proposal=proposal.to_dict(),
                error={"kind": type(exc).__name__, "message": str(exc)},
            )

        outcome = {
            "status": "committed",
            "session_id": str(request.get("session_id") or ""),
            "command_id": proposal.command_ids[0],
            "event_ids": [str(event_row.get("event_id") or event.get("event_id") or "")],
            "receipt": dict(receipt_row or receipt),
            "outbox_ids": outbox_ids,
            "terminal_plan": terminal_plan,
        }
        projection: dict[str, Any] | None = None
        try:
            projection = await self.projections.project(
                request=request,
                outcome=outcome,
            )
        except Exception as exc:  # noqa: BLE001 - 投影失败不回滚已提交事务
            _LOGGER.warning(
                "projection failed after commit (session=%s): %s",
                outcome["session_id"],
                exc,
            )
        return OrchestrationResult(
            status="success",
            stage="project",
            code="orchestration.committed",
            message="操作已提交。",
            recovery="",
            category=category,
            route=route.to_dict(),
            proposal=proposal.to_dict(),
            preview=preview,
            receipt=dict(receipt_row or receipt),
            event_ids=tuple(outcome["event_ids"]),
            outbox_ids=tuple(outbox_ids),
            terminal_plan=terminal_plan,
            projection=projection,
        )

    async def execute_application(
        self,
        ctx: RequestContext,
        handler: Any,
        command: Any,
        *,
        operation: str = "执行命令",
    ) -> CommandResult:
        """Execute one deterministic application command through one boundary.

        Existing domain repositories already own short, tested SQLite
        transactions.  Wrapping them in another connection-level transaction
        would create false atomicity.  This method is therefore the authority
        for request validation, exception mapping and result normalization;
        cross-domain writes use the ``TurnCommitPlan`` transaction
        adapters, while migrated services retain their internal
        transaction.
        """

        profiler = RequestProfiler(
            correlation_id=ctx.correlation_id,
            route=str(getattr(command, "action", "") or operation),
        )
        try:
            with profiler.stage("application"):
                result = handler(ctx, command)
                if hasattr(result, "__await__"):
                    result = await result
            if result is None:
                return CommandResult.ignored()
            if not isinstance(result, CommandResult):
                raise TypeError("application handler 必须返回 CommandResult")
            if not result.correlation_id:
                result.correlation_id = ctx.correlation_id
            return result
        except PermissionError as exc:
            return CommandResult.failed(
                CommandError(
                    code="command.permission_denied",
                    operation=operation,
                    reason=str(exc) or "当前账号没有执行此操作的权限。",
                    automatic_action="系统未修改任何数据。",
                    next_command="请联系主持人或管理员。",
                    correlation_id=ctx.correlation_id,
                    status_code=403,
                )
            )
        except LookupError as exc:
            return CommandResult.failed(
                CommandError(
                    code="command.not_found",
                    operation=operation,
                    reason=str(exc) or "找不到要操作的内容。",
                    automatic_action="系统未修改任何数据。",
                    next_command="请刷新当前状态后重新选择。",
                    correlation_id=ctx.correlation_id,
                    status_code=404,
                )
            )
        except ValueError as exc:
            return CommandResult.failed(
                CommandError(
                    code="command.invalid",
                    operation=operation,
                    reason=str(exc) or "输入内容不符合要求。",
                    automatic_action="系统未修改任何数据。",
                    next_command="/团 帮助",
                    correlation_id=ctx.correlation_id,
                )
            )
        except Exception as exc:  # noqa: BLE001 - safe boundary envelope
            _LOGGER.exception(
                "Application command failed: operation=%s correlation=%s",
                operation,
                ctx.correlation_id,
            )
            return CommandResult.failed(
                CommandError(
                    code="command.internal_failed",
                    operation=operation,
                    reason="系统内部处理失败。",
                    automatic_action="系统已保留原状态；未确认的步骤不会继续。",
                    next_command="/团 状态",
                    retryable=True,
                    correlation_id=ctx.correlation_id,
                    status_code=500,
                    technical={
                        "exception_type": type(exc).__name__,
                    },
                )
            )
        finally:
            _LOGGER.info("tavern.performance %s", profiler.json())

    def _build_event(
        self,
        envelope: Mapping[str, Any],
        proposal: ParsedProposal,
        command: Mapping[str, Any],
        changes: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """构造领域事件：payload 只含中文语义字段，技术字段留在信封层。"""

        affected = sorted(
            {
                str(change.get("state_scope") or "world")
                for change in _sequence(changes)
            }
        )
        return {
            "event_id": f"evt_{uuid.uuid4().hex[:16]}",
            "session_id": str(envelope.get("session_id") or ""),
            "seq": None,  # 存储层在事务内分配单调序号
            "type": str(command.get("event_type") or "event:ai.action"),
            "actor_ref": str(envelope.get("actor", {}).get("ref") or ""),
            "command_id": str(envelope.get("command_id") or ""),
            "causation_id": str(envelope.get("request_id") or ""),
            "correlation_id": str(envelope.get("request_id") or ""),
            "payload": {
                "title": str(command.get("audit_type") or proposal.task_type),
                "summary": proposal.narrative_draft[:200],
                "affected_modules": affected,
                "changes_count": len(_sequence(changes)),
            },
            "visibility": "public",
            "created_at": self.now_fn(),
        }

    def _terminal_context(
        self,
        request: Mapping[str, Any],
        state: Mapping[str, Any],
    ) -> dict[str, Any]:
        context = _mapping(request.get("context"))
        session = dict(_mapping(context.get("session")))
        session["state"] = state
        return {
            "world": _mapping(context.get("world")),
            "session": session,
            "party": _mapping(context.get("party")),
            "capabilities": _sequence(context.get("capabilities")),
            "terminal_conditions": _sequence(context.get("terminal_conditions")),
        }

    @staticmethod
    def _idempotency_key(
        request: Mapping[str, Any],
        proposal: ParsedProposal,
    ) -> str:
        explicit = str(request.get("idempotency_key") or "").strip()
        if explicit:
            return explicit[:240]
        digest = hashlib.sha256(
            proposal.payload_digest.encode("utf-8")
        ).hexdigest()
        return f"ai-proposal:{digest}"

    @staticmethod
    async def _safe_rollback(tx: Transaction) -> None:
        try:
            await tx.rollback()
        except Exception as exc:  # noqa: BLE001 - 回滚失败只记录
            _LOGGER.exception("rollback failed: %s", exc)

    @staticmethod
    def _reject(
        *,
        stage: str,
        code: str,
        message: str,
        recovery: str,
        technical_refs: Sequence[str] = (),
        category: str = "",
        route: dict[str, Any] | None = None,
    ) -> OrchestrationResult:
        return OrchestrationResult(
            status="rejected",
            stage=stage,
            code=code,
            message=message,
            recovery=recovery,
            technical_refs=tuple(technical_refs),
            category=category,
            route=route,
        )


class ApplicationCommandOrchestrator:
    """Deterministic BOT/Web command authority backed by ApplicationRouter."""

    def __init__(self, router: Any) -> None:
        self.router = router

    async def execute(
        self,
        ctx: RequestContext,
        command: Any,
    ) -> CommandResult:
        return await self.router.dispatch(ctx, command)


__all__ = [
    "ApplicationCommandOrchestrator",
    "ModelClient",
    "OrchestrationResult",
    "Orchestrator",
    "OutboxPlanner",
    "ProjectionService",
    "TerminalEvaluator",
    "Transaction",
    "TransactionFactory",
]
