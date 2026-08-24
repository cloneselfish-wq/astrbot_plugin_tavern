"""console session narrative-length mode read/write route."""

from __future__ import annotations

import functools
import inspect
from collections.abc import Mapping
from typing import Any, Callable

from ...narrative_modes import NARRATIVE_MODES
from ...visualization import visual_envelope
from ...visualization.envelopes import problem_from_exception
from ..errors import bad_request, forbidden
from . import WebRouteError, mapping, require_login, text, to_int
from .sessions import require_member
from ..surfaces.registry import resolve_surface_key


def _mode_route(kind: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        async def wrapped(*args: Any, **kwargs: Any) -> dict[str, Any]:
            try:
                result = func(*args, **kwargs)
                if inspect.isawaitable(result):
                    result = await result
                return result
            except Exception as exc:  # noqa: BLE001 - safe Web boundary
                status, problem = problem_from_exception(exc)
                state = (
                    "permission"
                    if status in {401, 403}
                    else "conflict"
                    if status == 409
                    else "error"
                )
                envelope = visual_envelope(
                    kind=kind,
                    data={},
                    revision=problem.preserved_revision,
                    summary={"label": "正文模式未能更新", "count": 0},
                    permissions={"can_view": state != "permission", "can_manage": False},
                    problems=[problem],
                    state=state,
                )
                return {"status": status, "body": envelope.to_dict()}

        return wrapped

    return decorate


@_mode_route("narrative_mode")
async def narrative_mode_view(
    principal: Mapping[str, Any],
    database: Any,
    *,
    query: Mapping[str, Any] | None = None,
    payload: Mapping[str, Any] | None = None,
    method: str = "GET",
    idempotency_key: str = "",
) -> dict[str, Any]:
    require_login(principal)
    query_map = mapping(query)
    body = mapping(payload)
    session_key = text(query_map.get("session_key") or body.get("session_key"))
    session_id = text(query_map.get("session_id") or body.get("session_id"))
    if session_key:
        session_id = text(
            resolve_surface_key(principal, "dashboard", session_key)
        )
        if not session_id:
            raise bad_request(
                "所选副本已经失效。",
                recovery="请返回副本列表后重新选择。",
            )
    if not session_id:
        raise bad_request(
            "缺少要调整的副本。",
            recovery="请从跑团现场重新选择副本。",
        )
    role = await require_member(database, session_id, principal)
    can_manage = role in {"dm", "admin"}
    if str(method or "GET").upper() == "POST":
        if not can_manage:
            raise forbidden(
                "只有当前副本主持人可以调整正文模式。",
                recovery="你仍可查看当前模式；请联系主持人进行调整。",
            )
        mode = text(body.get("mode")).lower()
        if mode not in NARRATIVE_MODES:
            raise bad_request(
                "正文模式无效。",
                recovery="请选择极简、平衡或史诗模式。",
            )
        expected = to_int(body.get("expected_revision"))
        if expected is None:
            raise WebRouteError(
                400,
                "narrative.mode.revision_required",
                "缺少当前正文模式版本。",
                "请刷新跑团现场后重新选择。",
            )
        request_key = text(idempotency_key or body.get("idempotency_key"))
        if not request_key:
            raise WebRouteError(
                400,
                "narrative.mode.idempotency_required",
                "本次调整缺少防重复凭证。",
                "请重新点击一次模式选项。",
            )
        view = await database.set_narrative_mode(
            session_id,
            mode,
            expected_revision=expected,
            actor_id=text(principal.get("username")),
            idempotency_key=request_key,
        )
    else:
        view = await database.get_narrative_mode(session_id)
    view = dict(view)
    view["can_manage"] = can_manage
    selected = NARRATIVE_MODES[str(view.get("mode") or "balanced")]
    envelope = visual_envelope(
        kind="narrative_mode",
        data=view,
        revision=int(view.get("revision") or 0),
        updated_at=text(view.get("updated_at")),
        summary={
            "label": selected.label,
            "summary": selected.description,
            "state": "下次生成生效",
            "count": len(NARRATIVE_MODES),
        },
        permissions={"can_view": True, "can_manage": can_manage},
        state="ready" if can_manage else "readonly",
        readonly=not can_manage,
    )
    return {"status": 200, "body": envelope.to_dict()}


__all__ = ["narrative_mode_view"]
