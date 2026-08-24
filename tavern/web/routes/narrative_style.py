"""Session narrative-style read/write route with safe audience projection."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ...visualization import visual_envelope
from ..errors import bad_request, forbidden
from . import WebRouteError, mapping, require_login, text, to_int
from .sessions import require_member
from ..surfaces.registry import resolve_surface_key


async def narrative_style_view(
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
        session_id = text(resolve_surface_key(principal, "dashboard", session_key))
    if not session_id:
        raise bad_request(
            "缺少要调整叙事文风的副本。",
            recovery="请从跑团现场重新选择副本。",
        )
    role = await require_member(database, session_id, principal)
    can_manage = role in {"dm", "admin"}
    if str(method or "GET").upper() == "POST":
        if not can_manage:
            raise forbidden(
                "只有当前副本主持人或管理员可以修改叙事文风。",
                recovery="你仍可查看当前档位；请联系主持人进行修改。",
            )
        expected = to_int(body.get("expected_revision"))
        if expected is None:
            raise WebRouteError(
                400,
                "narrative.style.revision_required",
                "缺少当前叙事文风版本。",
                "请刷新跑团现场后重试；你的草稿不会被新版本覆盖。",
            )
        request_key = text(idempotency_key or body.get("idempotency_key"))
        if not request_key:
            raise WebRouteError(
                400,
                "narrative.style.idempotency_required",
                "本次修改缺少防重复凭证。",
                "请重新点击一次“修改详细文风”。",
            )
        view = await database.set_narrative_style(
            session_id,
            text(body.get("preset_id")),
            text(body.get("custom_expectation")),
            expected_revision=expected,
            actor_id=text(principal.get("username")),
            idempotency_key=request_key,
            source_world_style_sha=text(body.get("source_world_style_sha")),
        )
    else:
        view = await database.get_narrative_style(
            session_id,
            can_manage=can_manage,
            include_private=can_manage,
        )
    view = dict(view)
    view["can_manage"] = can_manage
    envelope = visual_envelope(
        kind="narrative_style",
        data=view,
        revision=int(view.get("revision") or 0),
        updated_at=text(view.get("updated_at")),
        summary={
            "label": text(view.get("label"), "均衡"),
            "summary": text(view.get("public_summary")),
            "state": "下次生成生效",
            "count": len(view.get("options") or []),
        },
        permissions={"can_view": True, "can_manage": can_manage},
        state="ready" if can_manage else "readonly",
        readonly=not can_manage,
    )
    return {"status": 200, "body": envelope.to_dict()}


__all__ = ["narrative_style_view"]
