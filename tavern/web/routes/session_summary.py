from .visual_support import *

def _visual_route(kind: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        async def wrapped(*args: Any, **kwargs: Any) -> dict[str, Any]:
            try:
                result = func(*args, **kwargs)
                if inspect.isawaitable(result):
                    result = await result
                return result
            except Exception as exc:  # noqa: BLE001 - Web boundary
                status, problem = problem_from_exception(exc)
                state = (
                    "permission"
                    if status in {401, 403}
                    else "conflict"
                    if status == 409
                    else "error"
                )
                permissions = {
                    "can_view": state != "permission",
                    "can_manage": False,
                    "can_view_private": False,
                    "can_view_diagnostics": False,
                }
                envelope = visual_envelope(
                    kind=kind,
                    data={},
                    revision=problem.preserved_revision,
                    summary={"label": "数据暂时不可用", "count": 0},
                    permissions=permissions,
                    problems=[problem],
                    state=state,
                )
                return {"status": status, "body": envelope.to_dict()}

        return wrapped

    return decorate


async def _context(
    principal: Mapping[str, Any],
    database: Any,
    query: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], str, dict[str, Any], OpaqueKeyFactory]:
    require_login(principal)
    values = mapping(query)
    session_key = text(values.get("session_key"))
    session_id = (
        resolve_surface_key(principal, "dashboard", session_key)
        if session_key
        else ""
    )
    if session_key and not session_id:
        raise bad_request(
            "所选副本已经失效。",
            recovery="请返回总览重新选择副本。",
        )
    # ``session_id`` remains a migration adapter for legacy consumers.  The console
    # runtime obtains ``session_key`` from the principal-scoped dashboard
    # surface and never receives the stable database identifier.
    if not session_id:
        session_id = text(values.get("session_id"))
    if not session_id:
        raise bad_request(
            "缺少要查看的副本",
            recovery="请选择一个要查看的副本。",
        )
    session = await database.get_session(session_id)
    if session is None:
        raise not_found(
            "副本不存在或已删除",
            recovery="请刷新副本列表后重新选择。",
        )
    session = mapping(session)
    role = await require_member(database, session_id, principal)
    keys = OpaqueKeyFactory(
        scope=(
            f"console:{role}:{text(principal.get('username'))}:"
            f"{session_id}"
        )
    )
    return session, role, dict(principal), keys


def _conflict(
    kind: str,
    session: Mapping[str, Any],
    role: str,
    principal: Mapping[str, Any],
) -> dict[str, Any]:
    revision = session.get("revision")
    readonly = text(session.get("state")) == "finished"
    problem = VisualProblem(
        code="visual.revision.conflict",
        message="副本在你打开页面后已经更新。",
        recovery="请刷新当前板块并比较变化后重试。",
        retryable=True,
        preserved_revision=revision,
    )
    envelope = visual_envelope(
        kind=kind,
        data={"preserved": True},
        revision=revision,
        updated_at=text(session.get("updated_at")),
        summary={"label": "数据已更新", "count": 1},
        permissions={
            "can_view": True,
            "can_manage": role in {"dm", "admin"} and not readonly,
            "can_view_private": role in {"dm", "admin"},
            "can_view_diagnostics": bool(principal.get("is_admin")),
        },
        problems=[problem],
        state="conflict",
    )
    return {"status": 409, "body": envelope.to_dict()}


def _check_expected_revision(
    kind: str,
    session: Mapping[str, Any],
    role: str,
    principal: Mapping[str, Any],
    query: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    raw = mapping(query).get("expected_revision")
    if raw in (None, ""):
        return None
    expected = to_int(raw)
    if expected is None:
        raise WebRouteError(
            400,
            "visual.revision.invalid",
            "刷新依据不是有效版本。",
            "请重新打开当前板块。",
        )
    if int(expected) != int(session.get("revision") or 0):
        return _conflict(kind, session, role, principal)
    return None


def _response(envelope: Any, *, status: int = 200) -> dict[str, Any]:
    return {"status": int(status), "body": envelope.to_dict()}


def _history_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = row.get("payload_json")
    if isinstance(raw, Mapping):
        return dict(raw)
    if not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _history_round(row: Mapping[str, Any], payload: Mapping[str, Any]) -> int:
    for value in (
        payload.get("round_no"),
        payload.get("turn_no"),
        payload.get("round"),
        row.get("round_no"),
        row.get("turn_no"),
    ):
        parsed = to_int(value)
        if parsed is not None and parsed > 0:
            return parsed
    return 0


def _history_public_label(value: Any) -> str:
    candidate = display_label(value, fallback="")
    if not candidate:
        return ""
    lowered = candidate.casefold()
    if lowered.startswith(("participant-", "actor-", "user-", "private-")):
        return ""
    return candidate[:100]


async def _history_actor_names(
    database: Any,
    session_id: str,
) -> dict[str, str]:
    names: dict[str, str] = {}
    if not callable(getattr(database, "list_roster", None)):
        return names
    for raw in await database.list_roster(session_id) or ():
        item = mapping(raw)
        label = _history_public_label(
            item.get("character_name") or item.get("display_name")
        )
        if not label:
            continue
        for field in (
            "id",
            "group_user_id",
            "user_id",
            "private_user_id",
        ):
            identity = text(item.get(field))
            if identity:
                names[identity] = label
    return names


async def _history_visible_rows(
    database: Any,
    session_id: str,
    *,
    role: str,
    after_sequence: int = 0,
    limit: int = _HISTORY_SCAN_LIMIT + 1,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cursor = max(0, int(after_sequence))
    maximum = max(1, int(limit))
    while len(rows) < maximum:
        request_size = min(100, maximum - len(rows))
        batch = await database.list_session_events(
            session_id,
            after_seq=cursor,
            limit=request_size,
            visibility="" if role in {"dm", "admin"} else "public",
        )
        normalized = sorted(
            (
                mapping(item)
                for item in batch or ()
                if isinstance(item, Mapping)
                and (to_int(mapping(item).get("seq"), 0) or 0) > cursor
            ),
            key=lambda item: to_int(item.get("seq"), 0) or 0,
        )
        if not normalized:
            break
        rows.extend(normalized[: maximum - len(rows)])
        next_cursor = max(to_int(item.get("seq"), 0) or 0 for item in normalized)
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if len(normalized) < request_size:
            break
    return rows


__all__ = [name for name in globals() if not name.startswith('__')]


