"""D1-ARC-003：角色 / 参与者路由（纯服务，不依赖 Web 框架与 AstrBot）。

只消费 ``TavernDatabase`` 公开方法与既有投影函数，输出可直接 JSON
序列化的语义 DTO：

- ``character_list_view``：副本角色列表。普通玩家只能看到本人角色；
  DM/管理员可查看全部角色。
- ``character_detail_view``：单个角色详情。角色卡按 A/B/C 建卡阶段分组，
  附带分阶段补充（staged supplement）当前待确认 / 空 / 只读状态。
- ``supplement_offers_view``：副本补充提议面板（玩家仅本人，DM/管理员
  全副本）。

边界规则（D1-ARC-003 §4.2 / D1-UX-008 / D1-WEB-008）：

- 普通玩家视角不返回裸 DB 行、参与者稳定 ID、私聊来源、绑定码、
  原始卡 profile JSON 与内部字段键；
- 候选只展示中文名与一句话说明，内部候选 ID 仅授权角色可见；
- 归档副本整体 ``readonly``，补充区明确说明不可继续确认；
- 世界未启用分阶段建卡时补充区为 ``disabled``，不用空数组掩盖。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ...lifecycle import (
    CARD_STAGES,
    card_stage_state,
    card_template,
    field_stage,
    resolve_card_stage,
    stage_label,
    staged_creation,
)
from ...projections.character import (
    project_actor_view,
)
from ..errors import bad_request, forbidden, not_found
from .sessions import (
    require_member,
    resolve_viewer_participant,
    resolve_viewer_role,
)

from . import (
    ok,
    require_login,
    route_errors,
    text,
)

__all__ = [
    "character_detail_view",
    "character_list_view",
    "supplement_offers_view",
]


CARD_STATUS_LABELS = {
    "uncreated": "未建卡",
    "approved": "已批准",
    "pending": "待审核",
    "pending_review": "待审核",
    "rejected": "已驳回",
    "draft": "草稿",
}

PARTICIPATION_LABELS = {
    "active": "参与中",
    "reserved": "已占席",
    "standby": "候补",
    "away": "暂离",
    "retired": "已退场",
    "archived": "已归档",
}

def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _bool(value: Any) -> bool:
    return bool(value)


def _field_filled(profile: Mapping[str, Any], key: str) -> bool:
    value = profile.get(key)
    if isinstance(value, (list, tuple)):
        return bool(value)
    return bool(str(value or "").strip())


async def _world_snapshot(database: Any, session_id: str) -> dict[str, Any]:
    """读取副本世界快照；缺失时安全返回空 dict。"""
    try:
        instance = _mapping(await database.get_instance_config(session_id))
    except Exception:
        return {}
    snapshot = instance.get("world_snapshot")
    return _mapping(snapshot)


def _safe_template(world: Mapping[str, Any]) -> dict[str, Any]:
    """归一化角色模板；世界数据异常时不阻断角色列表。"""
    try:
        return _mapping(card_template(world))
    except (KeyError, TypeError, ValueError):
        return {}


def _actor_view(
    world: Mapping[str, Any],
    profile: Mapping[str, Any] | None,
    *,
    viewer_role: str,
    include_technical_refs: bool,
) -> dict[str, Any]:
    try:
        return project_actor_view(
            world,
            profile,
            viewer_role=viewer_role,
            include_technical_refs=include_technical_refs,
        )
    except (KeyError, TypeError, ValueError):
        return {
            "schema": "tavern-actor-view/1.0.0-rc10",
            "title": "",
            "subtitle": "",
            "sections": [],
            "problems": [
                {
                    "code": "projection.actor_view_failed",
                    "message": "角色卡投影失败",
                }
            ],
        }


def _stage_groups(
    template: Mapping[str, Any],
    profile: Mapping[str, Any] | None,
    actor_view: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """按 A/B/C 建卡阶段分组（16_STAGED_CHARACTER_CREATION §2、§10.2）。

    字段标签优先复用语义投影已解析的中文名；无法解析时给出明确错误，
    不把内部字段键展示给普通玩家。
    """

    profile = profile if isinstance(profile, Mapping) else {}
    actor_items: dict[str, dict[str, Any]] = {}
    for section in actor_view.get("sections") or ():
        for item in section.get("items") or ():
            key = str(item.get("field_id") or "").strip()
            if key:
                actor_items[key] = _mapping(item)
    result: list[dict[str, Any]] = []
    for stage in CARD_STAGES:
        fields: list[dict[str, Any]] = []
        for field in template.get("fields") or ():
            if not isinstance(field, Mapping):
                continue
            if str(field.get("type") or "").lower() == "derived":
                continue
            try:
                if field_stage(field) != stage:
                    continue
            except ValueError:
                continue
            key = str(field.get("key") or "").strip()
            if not key:
                continue
            projected = actor_items.get(key, {})
            label = text(projected.get("label"))
            if not label:
                label = text(field.get("label"))
            if not label:
                label = text(field.get("name"))
            if not label:
                label = "字段名称解析失败"
            private = bool(field.get("private")) or str(
                field.get("visibility") or "public"
            ).lower() in {"private", "dm", "author"}
            filled = _field_filled(profile, key)
            display_value = (
                text(projected.get("display_value"))
                if filled
                else ""
            )
            fields.append(
                {
                    "label": label,
                    "required": bool(field.get("required")),
                    "private": private,
                    "filled": filled,
                    "display_value": display_value,
                    "label_error": label == "字段名称解析失败",
                }
            )
        total = len(fields)
        filled_count = sum(1 for item in fields if item["filled"])
        required = [item for item in fields if item["required"]]
        required_filled = sum(1 for item in required if item["filled"])
        result.append(
            {
                "stage_id": stage,
                "stage_label": stage_label(template, stage),
                "total_fields": total,
                "filled_count": filled_count,
                "required_count": len(required),
                "required_filled_count": required_filled,
                "complete": required_filled == len(required) if required else True,
                "fields": fields,
            }
        )
    return result


def _participant_name(item: Mapping[str, Any]) -> str:
    return text(
        item.get("character_name")
        or item.get("display_name")
        or item.get("character_code")
    )


def _project_offer(
    offer: Mapping[str, Any],
    *,
    privileged: bool,
    readonly: bool,
    confirm_hint: str,
) -> dict[str, Any]:
    """补充提议安全视图：普通玩家剥离内部提议 ID、字段键与候选 ID。"""

    offer = _mapping(offer)
    candidates: list[dict[str, Any]] = []
    for raw in offer.get("candidates") or ():
        if not isinstance(raw, Mapping):
            continue
        item: dict[str, Any] = {
            "label": text(raw.get("label")),
            "description": text(raw.get("description")),
        }
        if privileged:
            item["id"] = text(raw.get("id"))
        if item["label"]:
            candidates.append(item)
    view: dict[str, Any] = {
        "field_label": text(offer.get("field_label")),
        "stage": text(offer.get("stage")),
        "stage_label": text(offer.get("stage_label")),
        "state": text(offer.get("state"), "offered"),
        "expired": bool(offer.get("expired")),
        "candidates": candidates,
        "free_text": bool(offer.get("free_text")),
        "fallback": bool(offer.get("fallback")),
        "offer_round": int(offer.get("offer_round") or 0),
        "expires_after_rounds": int(offer.get("expires_after_rounds") or 0),
        "confirm_entry": (
            {"channel": "private_command", "text": confirm_hint}
            if not privileged and not readonly
            else None
        ),
    }
    if privileged:
        view.update(
            {
                "offer_id": text(offer.get("offer_id")),
                "participant_id": text(offer.get("participant_id")),
                "field_key": text(offer.get("field_key")),
                "character_name": text(offer.get("character_name")),
                "delivery_status": text(offer.get("delivery_status")),
                "attempts": int(offer.get("attempts") or 0),
                "last_error": text(offer.get("last_error")),
                "trigger_source": text(offer.get("trigger_source")),
                "offer_no": int(offer.get("offer_no") or 0),
                "rejected_ids": [
                    text(item)
                    for item in offer.get("rejected_ids") or ()
                    if text(item)
                ],
            }
        )
    return view


def _supplement_view(
    *,
    template: Mapping[str, Any],
    offers: Sequence[Mapping[str, Any]],
    readonly: bool,
    privileged: bool,
    confirm_hint: str,
) -> dict[str, Any]:
    """补充区语义视图：disabled / readonly / pending / empty 四态。"""

    enabled = staged_creation(template)
    offer_views = [
        _project_offer(
            offer,
            privileged=privileged,
            readonly=readonly,
            confirm_hint=confirm_hint,
        )
        for offer in offers
        if isinstance(offer, Mapping)
    ]
    if not enabled:
        state = "disabled"
        message = "该世界不启用分阶段建卡，没有补充提议。"
    elif readonly:
        state = "readonly"
        message = "副本已归档，角色卡已锁定，无法继续确认补充。"
    elif offer_views:
        state = "pending"
        message = f"有 {len(offer_views)} 项角色补充等待确认。"
    else:
        state = "empty"
        message = "当前没有待确认的角色补充。"
    return {
        "enabled": enabled,
        "state": state,
        "state_label": {
            "disabled": "未启用",
            "readonly": "已锁定",
            "pending": "待确认",
            "empty": "暂无",
        }[state],
        "message": message,
        "pending_count": len(offer_views),
        "offers": offer_views,
    }


def _project_character_item(
    item: Mapping[str, Any],
    *,
    world: Mapping[str, Any],
    template: Mapping[str, Any],
    viewer_role: str,
    include_technical_refs: bool,
) -> dict[str, Any]:
    """把参与者行投影为安全角色卡条目（白名单，不返回裸行）。"""

    profile = _mapping(item.get("card_profile"))
    if not profile:
        draft = _mapping(item.get("draft_profile"))
        if draft:
            profile = draft
    actor_view = _actor_view(
        world,
        profile,
        viewer_role=viewer_role,
        include_technical_refs=include_technical_refs,
    )
    if not text(actor_view.get("title")):
        fallback = _participant_name(item)
        if fallback:
            actor_view["title"] = fallback
    stage_id = ""
    try:
        stage_id = resolve_card_stage(
            template,
            profile,
            row=item,
        )
    except (KeyError, TypeError, ValueError):
        stage_id = ""
    try:
        stage_state = card_stage_state(template, profile)
    except (KeyError, TypeError, ValueError):
        stage_state = {
            "stage": stage_id or "incomplete",
            "core_ready": False,
            "staged_pending": False,
            "stage_locked": False,
            "complete": False,
            "pending_count": 0,
            "pending_fields": [],
            "pending_stage_counts": {},
            "locked_fields": [],
            "missing_a": [],
            "missing_a_count": 0,
        }
    if stage_id and stage_id != stage_state.get("stage"):
        stage_state["stage"] = stage_id
    card_status = text(item.get("card_status"), "pending")
    participation = text(
        item.get("participation_status"),
        "active",
    )
    view: dict[str, Any] = {
        "name": _participant_name(item),
        "display_name": text(item.get("display_name")),
        "character_code": text(item.get("character_code")),
        "card_status": card_status,
        "card_status_label": CARD_STATUS_LABELS.get(
            card_status, "状态解析失败"
        ),
        "participation_status": participation,
        "participation_label": PARTICIPATION_LABELS.get(
            participation, "状态解析失败"
        ),
        "ready": _bool(item.get("ready")),
        "stage": stage_state,
        "actor_view": actor_view,
    }
    if include_technical_refs:
        view["technical"] = {
            "participant_id": text(item.get("id")),
            "card_version_no": text(item.get("card_version_no")),
            "template_version": text(item.get("card_template_version")),
            "binding_code": text(item.get("binding_code")),
        }
    return view


async def _load_context(
    database: Any,
    session_id: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """公共上下文：副本行、世界快照与模板。副本不存在抛 404。"""

    session_id = text(session_id)
    if not session_id:
        raise bad_request(
            "缺少 session_id",
            recovery="请选择一个要查看的副本。",
        )
    session = await database.get_session(session_id)
    if session is None:
        raise not_found(
            "副本不存在或已删除",
            recovery="请刷新副本列表后重新选择。",
        )
    world = await _world_snapshot(database, session_id)
    template = _safe_template(world)
    return _mapping(session), world, template


@route_errors
async def character_list_view(
    principal: Mapping[str, Any],
    database: Any,
    session_id: str,
    *,
    query: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """副本角色列表：普通玩家仅本人，DM/管理员可见全部。"""

    require_login(principal)
    _session, world, template = await _load_context(database, session_id)
    role = await resolve_viewer_role(database, session_id, principal)
    if not role:
        raise forbidden(
            "你不是该副本的成员，无法查看角色",
            recovery="请确认当前账号已加入该副本，或由主持人为你绑定角色卡。",
        )
    is_privileged = role in {"dm", "admin"}
    is_admin = bool(principal.get("is_admin"))
    rows = await database.list_roster(session_id)
    items: list[dict[str, Any]] = []
    if is_privileged:
        selected = [
            item for item in rows if isinstance(item, Mapping)
        ]
    else:
        own = await resolve_viewer_participant(
            database, session_id, text(principal.get("username"))
        )
        if own is None:
            selected = []
        else:
            own_id = text(own.get("id"))
            selected = [
                item
                for item in rows
                if isinstance(item, Mapping)
                and text(item.get("id")) == own_id
            ]
    for item in selected:
        items.append(
            _project_character_item(
                item,
                world=world,
                template=template,
                viewer_role=role if is_privileged else "character",
                include_technical_refs=is_admin,
            )
        )
    return ok(
        {
            "session_id": session_id,
            "items": items,
            "count": len(items),
            "viewer_role": role,
            "permissions": {
                "can_view_all": is_privileged,
                "can_view_own": True,
                "role_source": text(principal.get("role_source"), "unmapped"),
            },
        }
    )


def _resolve_participant_ref(
    rows: Sequence[Mapping[str, Any]],
    participant_ref: str,
) -> Mapping[str, Any]:
    """DM/管理员按 ID、代号、角色名或显示名解析参与者。"""

    ref = text(participant_ref)
    if not ref:
        raise bad_request(
            "缺少角色标识",
            recovery="请从角色列表中选择一个角色。",
        )
    lowered = ref.casefold()
    matches = []
    for item in rows:
        if not isinstance(item, Mapping):
            continue
        identities = (
            text(item.get("id")),
            text(item.get("character_code")),
            text(item.get("character_name")),
            text(item.get("display_name")),
        )
        if any(identity.casefold() == lowered for identity in identities if identity):
            matches.append(item)
    if not matches:
        raise not_found(
            "角色不存在或已离开副本",
            recovery="请刷新角色列表后重新选择。",
        )
    if len(matches) > 1:
        raise bad_request(
            "存在多个同名角色，无法确定要查看哪一个",
            recovery="请使用更完整的角色名，或从角色列表中选择。",
        )
    return matches[0]


@route_errors
async def character_detail_view(
    principal: Mapping[str, Any],
    database: Any,
    session_id: str,
    *,
    participant_ref: str = "",
    query: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """单个角色详情：A/B/C 阶段分组 + 补充区（待确认 / 空 / 只读）。"""

    require_login(principal)
    session, world, template = await _load_context(database, session_id)
    role = await require_member(database, session_id, principal)
    is_privileged = role in {"dm", "admin"}
    is_admin = bool(principal.get("is_admin"))
    rows = await database.list_roster(session_id)
    if is_privileged:
        target = _resolve_participant_ref(rows, participant_ref)
    else:
        own = await resolve_viewer_participant(
            database, session_id, text(principal.get("username"))
        )
        if own is None:
            raise not_found(
                "你还没有加入该副本的角色",
                recovery="请先加入副本并完成建卡，再查看角色详情。",
            )
        own_id = text(own.get("id"))
        target = next(
            (
                item
                for item in rows
                if isinstance(item, Mapping) and text(item.get("id")) == own_id
            ),
            {},
        )
    if not target:
        raise not_found(
            "没有找到你的角色数据",
            recovery="请刷新角色列表后重试；若仍失败，请联系主持人。",
        )
    profile = _mapping(target.get("card_profile"))
    if not profile:
        profile = _mapping(target.get("draft_profile"))
    item = _project_character_item(
        target,
        world=world,
        template=template,
        viewer_role=role if is_privileged else "character",
        include_technical_refs=is_admin,
    )
    item["stage_groups"] = _stage_groups(
        template,
        profile,
        item["actor_view"],
    )
    archive = await database.get_session_archive_view(session_id)
    readonly = bool(
        isinstance(archive, Mapping) and archive.get("readonly")
    ) or text(session.get("state")) == "finished"
    offers = await database.list_supplement_offers(
        session_id,
        participant_id=text(target.get("id")),
        viewer_role=role if is_privileged else "player",
    )
    confirm_hint = "请私聊 BOT 发送「/团 当前」查看待确认补充，并按序号回复确认。"
    item["supplement"] = _supplement_view(
        template=template,
        offers=offers,
        readonly=readonly,
        privileged=is_privileged,
        confirm_hint=confirm_hint,
    )
    item["readonly"] = readonly
    item["permissions"] = {
        "viewer_role": role,
        "can_view_all": is_privileged,
        "can_view_own": not is_privileged,
        "can_confirm_supplement": not readonly and not is_privileged,
        "confirm_hint": confirm_hint if not readonly else "",
    }
    return ok({"session_id": session_id, "item": item})


@route_errors
async def supplement_offers_view(
    principal: Mapping[str, Any],
    database: Any,
    session_id: str,
    *,
    query: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """副本补充提议面板：玩家仅本人，DM/管理员全副本。"""

    require_login(principal)
    session, world, template = await _load_context(database, session_id)
    role = await require_member(database, session_id, principal)
    is_privileged = role in {"dm", "admin"}
    participant_id = ""
    if not is_privileged:
        own = await resolve_viewer_participant(
            database, session_id, text(principal.get("username"))
        )
        participant_id = text(own.get("id")) if own else ""
    archive = await database.get_session_archive_view(session_id)
    readonly = bool(
        isinstance(archive, Mapping) and archive.get("readonly")
    ) or text(session.get("state")) == "finished"
    offers = await database.list_supplement_offers(
        session_id,
        participant_id=participant_id,
        viewer_role=role if is_privileged else "player",
    )
    confirm_hint = "请私聊 BOT 发送「/团 当前」查看待确认补充，并按序号回复确认。"
    supplement = _supplement_view(
        template=template,
        offers=offers,
        readonly=readonly,
        privileged=is_privileged,
        confirm_hint=confirm_hint,
    )
    return ok(
        {
            "session_id": session_id,
            "supplement": supplement,
            "viewer_role": role,
            "readonly": readonly,
            "permissions": {
                "can_view_all": is_privileged,
                "can_confirm_supplement": not readonly and not is_privileged,
                "confirm_hint": confirm_hint if not readonly else "",
            },
        }
    )

__all__ = [name for name in globals() if not name.startswith('__')]

