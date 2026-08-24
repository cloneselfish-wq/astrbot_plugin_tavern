"""D1-ARC-003：资产与经济纯路由。

只消费 ``TavernDatabase`` 公开接口与既有语义投影（``project_resource_view``
等），不接触 AstrBot、Web 框架或平台消息对象；返回值是可 JSON 序列化的
路由信封 ``{"status": ..., "body": {...}}``，错误统一由 ``route_errors``
转成标准信封（code / message / recovery / correlation_id）。

边界（D1-ARC-003 §4.2 / §4.3，D1-WEB-003）：

- 普通玩家只能看到自己角色名下的物品与钱包（按副本参与者身份解析），
  任何视图都不出现 ``owner_ref`` / ``currency_id`` / ``item_id`` /
  ``actor_id`` 等内部标识；管理员额外获得技术引用。
- 经济写操作（开关、调账）按 DM / 管理员 / 副本 host / mod 权限放行；
  普通成员一律 403。
- 归档副本（``finished``）拒绝全部写操作并标记 ``readonly``。
- 世界快照缺失时不崩溃：返回能力未声明 + 明确 ``problems`` 条目，
  由前端按“明确错误”状态展示，不用占位名称掩盖。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Callable

from ...errors import InsufficientFundsError
from ...item_catalog import item_definition, item_label
from ...database_support import DatabaseNotFoundError
from ...projections.world import (
    project_resource_view,
    world_has_capability,
)

from . import (
    WebRouteError,
    actor_id,
    flag,
    mapping,
    ok,
    require_login,
    route_errors,
    text,
    to_int,
)
from .sessions import (
    require_member,
    resolve_viewer_participant,
    resolve_viewer_role,
)

__all__ = [
    "assets_view",
    "economy_adjust",
    "economy_migrate_world",
    "economy_set_enabled",
    "economy_summary",
    "economy_transactions",
]

#: 非角色钱包所有者的中文兜底标签（仅展示，不含内部 ID）。
_OWNER_TYPE_LABELS = {
    "party": "队伍",
    "shop": "商店",
    "npc": "NPC",
    "world": "世界",
    "system": "系统",
}

#: 玩家不可见的内部字段（防御性剥离，与投影器语义一致）。
_INTERNAL_FIELDS = frozenset(
    {
        "owner_type",
        "owner_ref",
        "currency_id",
        "item_id",
        "actor_id",
        "operation_id",
        "from_owner_type",
        "from_owner_ref",
        "to_owner_type",
        "to_owner_ref",
        "balance_before",
        "balance_after",
        "source",
        "session_id",
        "extensions_json",
        "sort_order",
        "precision",
        "allow_negative",
        "transferable",
        "exchangeable",
        "public",
        "rowid",
    }
)

_KIND_LABELS = {
    "adjust": "调整",
    "credit": "入账",
    "debit": "扣款",
    "transfer": "转账",
    "exchange": "兑换",
    "purchase": "购买",
    "grant": "授予",
    "refund": "退款",
    "reward": "奖励",
    "payment": "支付",
}

_STATUS_LABELS = {
    "committed": "已入账",
    "reverted": "已回滚",
    "failed": "失败",
    "pending": "处理中",
}


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _bool(value: Any) -> bool:
    return bool(value)


async def _require_session(database: Any, session_id: str) -> dict[str, Any]:
    session_id = _text(session_id)
    if not session_id:
        raise WebRouteError(
            400,
            "assets.session_missing",
            "缺少 session_id。",
            "请先选择一个要查看的副本。",
        )
    try:
        session = await database.get_session(session_id)
    except DatabaseNotFoundError:
        raise WebRouteError(
            404,
            "assets.session_not_found",
            "副本不存在或已删除。",
            "请刷新副本列表后重新选择。",
        ) from None
    if session is None:
        raise WebRouteError(
            404,
            "assets.session_not_found",
            "副本不存在或已删除。",
            "请刷新副本列表后重新选择。",
        )
    return dict(session) if isinstance(session, Mapping) else {}


async def _world_snapshot(
    database: Any,
    session_id: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """读取副本冻结世界快照；缺失时返回空世界与明确 problems 条目。"""
    try:
        instance = await database.get_instance_config(session_id)
    except Exception:
        instance = {}
    world = (
        dict(instance.get("world_snapshot"))
        if isinstance(instance, Mapping)
        and isinstance(instance.get("world_snapshot"), Mapping)
        else {}
    )
    problems: list[dict[str, Any]] = []
    if not world:
        problems.append(
            {
                "code": "assets.world_snapshot_missing",
                "message": "世界包快照缺失，无法解析物品与货币定义；"
                "资产能力暂不展示。",
            }
        )
    return world, problems


def _is_readonly(session: Mapping[str, Any]) -> bool:
    return _text(session.get("state")) == "finished"


def _raise_if_readonly(session: Mapping[str, Any]) -> None:
    if _is_readonly(session):
        raise WebRouteError(
            409,
            "assets.readonly",
            "该副本已归档并处于只读状态，无法修改资产。",
            "请选择其他副本，或从最终存档克隆新副本后继续。",
        )


async def _privileged_role(
    database: Any,
    session_id: str,
    principal: Mapping[str, Any],
    *,
    allow_moderator: bool = False,
) -> str:
    """返回 dm / admin / moderator；无权限直接抛 403（fail closed）。

    与 C6 ``can_adjust_economy`` 一致：moderator 依赖副本授权记录，不要求
    同时是副本参与者（host / mod 可能由管理员后台指派）。
    """
    role = await resolve_viewer_role(database, session_id, principal)
    if role in {"dm", "admin"}:
        return role
    if allow_moderator:
        username = _text(principal.get("username"))
        if username:
            try:
                grants = await database.list_permission_grants(session_id)
            except Exception:
                grants = []
            for item in grants or ():
                if not isinstance(item, Mapping):
                    continue
                if (
                    _text(item.get("user_id")) == username
                    and _text(item.get("role")) == "moderator"
                ):
                    return "moderator"
    raise WebRouteError(
        403,
        "assets.forbidden",
        "你没有执行该资产操作的权限。",
        "请确认当前账号是副本 DM、管理员，或持有 host / mod 权限。",
    )


def _enrich_item_rows(
    rows: list[Mapping[str, Any]],
    world: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """附加物品显示名与描述；世界快照缺失时保持无 label（投影器会给出
    display_error，绝不把 item_id 当显示名回退）。"""
    enriched: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        item = dict(raw)
        item_id = _text(item.get("item_id"))
        if world and item_id:
            definition = item_definition(world, item_id)
            item["label"] = item_label(world, item_id)
            if isinstance(definition, Mapping):
                item["category"] = _text(definition.get("kind"))
                item["description"] = _text(definition.get("description"))
        item["container_label"] = _text(item.get("container"))
        enriched.append(item)
    return enriched


def _wallet_owner_labels(
    wallets: list[Mapping[str, Any]],
    roster: list[Mapping[str, Any]],
) -> dict[str, str]:
    """按类型化所有者构造展示名索引（character:角色名，其余按类型兜底）。"""
    labels: dict[str, str] = {}
    for item in roster or ():
        if not isinstance(item, Mapping):
            continue
        participant_id = _text(item.get("id"))
        if not participant_id:
            continue
        name = _text(
            item.get("character_name")
            or item.get("display_name")
            or item.get("character_code")
        )
        if name:
            labels[f"character:{participant_id}"] = name
    for wallet in wallets or ():
        if not isinstance(wallet, Mapping):
            continue
        owner_type = _text(wallet.get("owner_type"))
        owner_ref = _text(wallet.get("owner_ref"))
        if not owner_type or not owner_ref:
            continue
        key = f"{owner_type}:{owner_ref}"
        if key not in labels and owner_type in _OWNER_TYPE_LABELS:
            labels[key] = _OWNER_TYPE_LABELS[owner_type]
    return labels


def _strip_internal(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in dict(payload).items()
        if key not in _INTERNAL_FIELDS
    }


@route_errors
async def assets_view(
    principal: Mapping[str, Any],
    database: Any,
    *,
    query: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """副本资产视图：普通玩家只见本人角色名下背包与钱包。"""
    require_login(principal)
    session_id = _text(mapping(query).get("session_id"))
    session = await _require_session(database, session_id)
    role = await require_member(database, session_id, principal)
    is_admin = bool(principal.get("is_admin"))
    world, problems = await _world_snapshot(database, session_id)
    page = max(1, to_int(mapping(query).get("page"), 1) or 1)
    page_size = max(
        1,
        min(100, to_int(mapping(query).get("page_size"), 100) or 100),
    )

    owner_labels: dict[str, str] = {}
    item_instances: list[dict[str, Any]] = []
    inventory_page = {
        "page": page,
        "page_size": page_size,
        "total": 0,
        "pages": 1,
    }
    economy_payload: dict[str, Any] = {}
    viewer_role = "player"
    include_technical_refs = False

    try:
        summary = await database.economy_summary(session_id)
    except Exception:
        summary = {}
    if not isinstance(summary, Mapping):
        summary = {}

    def empty_inventory_page() -> dict[str, Any]:
        return {
            "items": [],
            "page": page,
            "page_size": page_size,
            "total": 0,
            "pages": 1,
        }

    if role == "player":
        participant = await resolve_viewer_participant(
            database, session_id, _text(principal.get("username"))
        )
        if not participant:
            raise WebRouteError(
                403,
                "assets.participant_unresolved",
                "无法解析你的角色身份。",
                "请先由主持人绑定角色卡后重试。",
            )
        participant_id = _text(participant.get("id"))
        try:
            inventory_page = await database.page_item_instances(
                session_id=session_id,
                owner_ref=participant_id,
                page=page,
                page_size=page_size,
            )
        except Exception:
            inventory_page = empty_inventory_page()
        if not isinstance(inventory_page, Mapping):
            inventory_page = empty_inventory_page()
        item_instances = _enrich_item_rows(
            inventory_page.get("items") or [],
            world,
        )
        owner_name = _text(
            participant.get("character_name")
            or participant.get("display_name")
            or participant.get("character_code")
        )
        if owner_name:
            owner_labels[f"character:{participant_id}"] = owner_name
        wallets = [
            dict(item)
            for item in (summary.get("wallets") or [])
            if isinstance(item, Mapping)
            and _text(item.get("owner_type")) == "character"
            and _text(item.get("owner_ref")) == participant_id
        ]
        economy_payload = {
            "enabled": bool(summary.get("enabled")),
            "currencies": [
                dict(item)
                for item in (summary.get("currencies") or [])
                if isinstance(item, Mapping)
            ],
            "wallets": wallets,
        }
    else:
        try:
            roster = await database.list_roster(session_id)
        except Exception:
            roster = []
        try:
            inventory_page = await database.page_item_instances(
                session_id=session_id,
                page=page,
                page_size=page_size,
            )
        except Exception:
            inventory_page = empty_inventory_page()
        if not isinstance(inventory_page, Mapping):
            inventory_page = empty_inventory_page()
        item_instances = _enrich_item_rows(
            inventory_page.get("items") or [],
            world,
        )
        owner_labels = _wallet_owner_labels(
            summary.get("wallets") or [], roster or []
        )
        economy_payload = {
            "enabled": bool(summary.get("enabled")),
            "currencies": [
                dict(item)
                for item in (summary.get("currencies") or [])
                if isinstance(item, Mapping)
            ],
            "wallets": [
                dict(item)
                for item in (summary.get("wallets") or [])
                if isinstance(item, Mapping)
            ],
        }
        viewer_role = "admin" if is_admin else "dm"
        include_technical_refs = is_admin

    try:
        resource = project_resource_view(
            world,
            item_instances=item_instances,
            economy=economy_payload,
            owner_labels=owner_labels,
            viewer_role=viewer_role,
            include_technical_refs=include_technical_refs,
        )
    except ValueError as exc:
        raise WebRouteError(
            500,
            "assets.projection_failed",
            "资产数据投影失败。",
            "请刷新后重试；若仍失败，请联系管理员检查副本数据。",
        ) from exc
    if isinstance(resource, Mapping):
        existing = resource.get("problems") or []
        if isinstance(existing, list):
            problems.extend(existing)
        resource["problems"] = problems

    return ok(
        {
            "session_id": session_id,
            "schema": resource.get("schema") if isinstance(resource, Mapping) else "",
            "resource": resource,
            "pagination": {
                key: int(inventory_page.get(key) or default)
                for key, default in (
                    ("page", page),
                    ("page_size", page_size),
                    ("total", 0),
                    ("pages", 1),
                )
            },
            "readonly": _is_readonly(session),
            "permissions": {
                "can_adjust_economy": role in {"dm", "admin"} and not _is_readonly(session),
                "role_source": _text(principal.get("role_source"), "unmapped"),
            },
        }
    )


@route_errors
async def economy_summary(
    principal: Mapping[str, Any],
    database: Any,
    *,
    query: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """经济概览：普通玩家只见本人钱包与本人流水，且不含内部标识。"""
    require_login(principal)
    session_id = _text(mapping(query).get("session_id"))
    session = await _require_session(database, session_id)
    role = await require_member(database, session_id, principal)
    is_privileged = role in {"dm", "admin"}
    world, _problems = await _world_snapshot(database, session_id)
    summary = await database.economy_summary(session_id)
    if not isinstance(summary, Mapping):
        summary = {}

    if not is_privileged:
        participant = await resolve_viewer_participant(
            database, session_id, _text(principal.get("username"))
        )
        participant_id = _text(participant.get("id")) if participant else ""
        summary = dict(summary)
        capability = dict(summary.get("capability") or {})
        capability.pop("installed_world_ref", None)
        summary["capability"] = capability
        summary["wallets"] = [
            _strip_internal(item)
            for item in (summary.get("wallets") or [])
            if isinstance(item, Mapping)
            and _text(item.get("owner_type")) == "character"
            and _text(item.get("owner_ref")) == participant_id
        ]
        summary["currencies"] = [
            {
                "label": _text(item.get("label") or item.get("name")),
                "short_label": _text(
                    item.get("short_label") or item.get("short_name")
                ),
                "icon": _text(item.get("icon")),
            }
            for item in (summary.get("currencies") or [])
            if isinstance(item, Mapping)
        ]
        summary["recent"] = [
            _strip_internal(
                {
                    "kind_label": _KIND_LABELS.get(
                        _text(item.get("kind")), "资金变动"
                    ),
                    "currency_label": _text(item.get("currency_label")),
                    "formatted_amount": _text(item.get("formatted_amount")),
                    "reason": _text(item.get("reason")),
                    "status_label": _STATUS_LABELS.get(
                        _text(item.get("status")), "处理中"
                    ),
                    "created_at": _text(item.get("created_at")),
                }
            )
            for item in (summary.get("recent") or [])
            if isinstance(item, Mapping)
        ]
        summary["exchange_rules"] = []

    return ok(
        {
            "session_id": session_id,
            "economy": summary,
            "capability": {
                "economy": world_has_capability(world, "economy"),
                "inventory": world_has_capability(world, "inventory"),
            },
            "readonly": _is_readonly(session),
            "permissions": {
                "can_adjust": is_privileged and not _is_readonly(session),
                "role_source": _text(principal.get("role_source"), "unmapped"),
            },
        }
    )


@route_errors
async def economy_transactions(
    principal: Mapping[str, Any],
    database: Any,
    *,
    query: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """经济流水：普通玩家只见本人角色相关流水，剥离内部标识。"""
    require_login(principal)
    session_id = _text(mapping(query).get("session_id"))
    await _require_session(database, session_id)
    role = await require_member(database, session_id, principal)
    is_privileged = role in {"dm", "admin"}
    limit = max(1, min(200, to_int(mapping(query).get("limit"), 100) or 100))
    rows = await database.economy_list_transactions(session_id, limit)

    if is_privileged:
        items = [
            dict(item)
            for item in rows
            if isinstance(item, Mapping)
        ]
    else:
        participant = await resolve_viewer_participant(
            database, session_id, _text(principal.get("username"))
        )
        participant_id = _text(participant.get("id")) if participant else ""
        items = []
        for item in rows:
            if not isinstance(item, Mapping):
                continue
            involved = (
                _text(item.get("from_owner_type")) == "character"
                and _text(item.get("from_owner_ref")) == participant_id
            ) or (
                _text(item.get("to_owner_type")) == "character"
                and _text(item.get("to_owner_ref")) == participant_id
            )
            if not involved:
                continue
            items.append(
                _strip_internal(
                    {
                        "kind_label": _KIND_LABELS.get(
                            _text(item.get("kind")), "资金变动"
                        ),
                        "currency_label": _text(item.get("currency_label")),
                        "formatted_amount": _text(item.get("formatted_amount")),
                        "reason": _text(item.get("reason")),
                        "status_label": _STATUS_LABELS.get(
                            _text(item.get("status")), "处理中"
                        ),
                        "created_at": _text(item.get("created_at")),
                    }
                )
            )

    return ok(
        {
            "session_id": session_id,
            "items": items,
            "count": len(items),
        }
    )


@route_errors
async def economy_migrate_world(
    principal: Mapping[str, Any],
    database: Any,
    *,
    payload: Mapping[str, Any] | None = None,
    actor: str = "",
    publish: Callable[[Mapping[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """Explicitly migrate a frozen session world after an immutable backup."""

    require_login(principal)
    data = mapping(payload)
    session_id = _text(data.get("session_id"))
    session = await _require_session(database, session_id)
    role = await _privileged_role(database, session_id, principal)
    if role != "admin" and not bool(principal.get("is_admin")):
        raise WebRouteError(
            403,
            "assets.world_migration.admin_required",
            "迁移冻结世界失败：当前身份不是管理员。",
            "系统未修改副本；请由管理员在副本暂停后重新发起迁移。",
        )
    _raise_if_readonly(session)
    candidate_world_ref = _text(data.get("candidate_world_ref"))
    if not candidate_world_ref:
        raise WebRouteError(
            400,
            "assets.world_migration.candidate_required",
            "迁移冻结世界失败：未选择候选世界 revision。",
            "系统未修改副本；请刷新世界库并重新选择目标版本。",
        )
    expected_revision = to_int(data.get("expected_revision"), 0)
    if expected_revision <= 0:
        raise WebRouteError(
            400,
            "assets.world_migration.revision_required",
            "迁移冻结世界失败：缺少当前副本 revision。",
            "系统未修改副本；请刷新副本详情后重新确认。",
        )
    operation_id = _text(data.get("operation_id"))
    if not operation_id:
        raise WebRouteError(
            400,
            "assets.world_migration.operation_required",
            "迁移冻结世界失败：缺少幂等操作号。",
            "系统未修改副本；请刷新页面后重新发起迁移。",
        )
    result = await database.migrate_session_world(
        session_id,
        candidate_world_ref,
        _text(actor) or actor_id(principal),
        expected_revision=expected_revision,
        operation_id=operation_id,
        confirmation=_text(data.get("confirmation")),
    )
    if publish is not None:
        publish(
            {
                "type": "world",
                "action": "snapshot_migrated",
                "session_id": session_id,
            }
        )
    return ok(
        {
            "result": result,
            "message": (
                "冻结世界已在不可覆盖备份后迁移；"
                "请刷新副本并检查经济播种收据。"
            ),
        }
    )


@route_errors
async def economy_set_enabled(
    principal: Mapping[str, Any],
    database: Any,
    *,
    payload: Mapping[str, Any] | None = None,
    actor: str = "",
    audit: Callable[..., Any] | None = None,
    publish: Callable[[Mapping[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """开关副本经济系统（DM / 管理员）。"""
    require_login(principal)
    data = mapping(payload)
    session_id = _text(data.get("session_id"))
    session = await _require_session(database, session_id)
    await _privileged_role(database, session_id, principal)
    _raise_if_readonly(session)
    if "enabled" not in data:
        raise WebRouteError(
            400,
            "assets.economy.enabled_missing",
            "缺少经济开关状态。",
            "请选择“启用”或“停用”后重试。",
        )
    enabled = _bool(data.get("enabled"))
    actor_name = _text(actor) or actor_id(principal)
    result = await database.set_economy_enabled(
        session_id, enabled, actor_name
    )
    if audit is not None:
        try:
            await audit(
                session_id,
                f"web:{_text(principal.get('username'))}",
                "economy.set_enabled",
                "",
                {"enabled": enabled},
            )
        except Exception:
            pass
    if publish is not None:
        publish(
            {
                "type": "economy",
                "action": "enabled",
                "session_id": session_id,
                "enabled": enabled,
            }
        )
    return ok(
        {
            "session_id": session_id,
            "enabled": bool(
                dict(result).get("enabled", enabled)
                if isinstance(result, Mapping)
                else enabled
            ),
            "message": "经济系统已启用" if enabled else "经济系统已停用",
        }
    )


@route_errors
async def economy_adjust(
    principal: Mapping[str, Any],
    database: Any,
    *,
    payload: Mapping[str, Any] | None = None,
    actor: str = "",
    audit: Callable[..., Any] | None = None,
    publish: Callable[[Mapping[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """调整钱包余额（DM / 管理员 / host / mod；按 operation_id 幂等）。"""
    require_login(principal)
    data = mapping(payload)
    session_id = _text(data.get("session_id"))
    session = await _require_session(database, session_id)
    await _privileged_role(
        database, session_id, principal, allow_moderator=True
    )
    _raise_if_readonly(session)

    kind = _text(data.get("kind"), "adjust").lower()
    if kind not in {"adjust", "credit", "debit", "transfer"}:
        raise WebRouteError(
            400,
            "assets.economy.unsupported_kind",
            "不支持的经济操作类型。",
            "请选择调整、入账、扣款或转账后重试。",
        )
    currency_id = _text(data.get("currency_id"))
    if not currency_id:
        raise WebRouteError(
            400,
            "assets.economy.currency_missing",
            "缺少货币。",
            "请先选择要调整的货币。",
        )
    amount = data.get("amount")
    try:
        amount_int = int(amount)
    except (TypeError, ValueError, OverflowError):
        raise WebRouteError(
            400,
            "assets.economy.amount_invalid",
            "金额格式不正确。",
            "请输入整数金额后重试。",
        )
    if amount_int == 0:
        raise WebRouteError(
            400,
            "assets.economy.amount_zero",
            "调整金额不能为 0。",
            "请输入非零金额后重试。",
        )
    def _owner(prefix: str) -> tuple[str, str] | None:
        owner_type = _text(data.get(f"{prefix}_owner_type"))
        owner_ref = _text(data.get(f"{prefix}_owner_ref"))
        if owner_type and owner_ref:
            return (owner_type, owner_ref)
        return None

    from_owner = _owner("from")
    to_owner = _owner("to")
    if kind == "transfer" and (not from_owner or not to_owner):
        raise WebRouteError(
            400,
            "assets.economy.transfer_owners_required",
            "转账必须同时提供转出方与转入方。",
            "请补充转出与转入方后重试。",
        )
    if not from_owner and not to_owner:
        raise WebRouteError(
            400,
            "assets.economy.owner_required",
            "经济操作至少需要一个钱包方向。",
            "请指定转出或转入方后重试。",
        )

    actor_name = _text(actor) or actor_id(principal)
    operation_id = _text(
        data.get("operation_id")
    ) or f"web:economy:{session_id}:{actor_name}"
    try:
        result = await database.economy_apply(
            session_id=session_id,
            operation_id=operation_id,
            kind=kind,
            currency_id=currency_id,
            amount=amount_int,
            from_owner=from_owner,
            to_owner=to_owner,
            reason=_text(data.get("reason")),
            source="web",
            actor_id=actor_name,
        )
    except InsufficientFundsError as exc:
        raise WebRouteError(
            400,
            "assets.economy.insufficient_funds",
            str(exc),
            "请先核对可用余额后重试。",
        ) from exc
    if isinstance(result, Mapping) and result.get("ok") is False:
        raise WebRouteError(
            400,
            "assets.economy.insufficient_funds",
            str(result.get("message") or "余额不足，操作未生效。"),
            "请先核对可用余额后重试。",
        )
    if audit is not None:
        try:
            await audit(
                session_id,
                f"web:{_text(principal.get('username'))}",
                "economy.apply",
                operation_id,
                {"kind": kind, "currency_id": currency_id, "amount": amount_int},
            )
        except Exception:
            pass
    if publish is not None:
        publish(
            {
                "type": "economy",
                "action": "apply",
                "session_id": session_id,
                "kind": kind,
            }
        )
    return ok(
        {
            "session_id": session_id,
            "ok": True,
            "message": "资金变动已生效。",
            "result": dict(result) if isinstance(result, Mapping) else {},
        }
    )
