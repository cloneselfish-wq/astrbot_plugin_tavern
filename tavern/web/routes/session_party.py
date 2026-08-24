from .visual_support import *
from .session_summary import *
from .actor_fate import actor_fate_consent_view
from ..surfaces.registry import _session_lifecycle_actions

def _history_project_rows(
    rows: list[dict[str, Any]],
    *,
    keys: OpaqueKeyFactory,
    actor_names: Mapping[str, str],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    projected: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for raw in rows:
        safe = project_session_event(raw, is_admin=False)
        sequence = to_int(safe.get("seq"), 0) or 0
        payload = _history_payload(raw)
        category = text(safe.get("category"), "system") or "system"
        actor_identity = text(raw.get("actor_ref")) or "system"
        actor_label = _history_public_label(
            payload.get("actor_name")
            or payload.get("actor_label")
            or payload.get("character_name")
            or payload.get("display_name")
        )
        if not actor_label:
            actor_label = text(actor_names.get(actor_identity))
        if not actor_label:
            actor_label = "系统" if actor_identity == "system" else "小队成员"
        round_no = _history_round(raw, payload)
        type_label = CATEGORY_LABELS.get(category, "副本状态")
        item: dict[str, Any] = {
            "key": keys.key("historyevent", sequence),
            "sequence": sequence,
            "category": category,
            "title": text(safe.get("title"))[:100],
            "summary": text(safe.get("summary"))[:240],
            "visual_kinds": list(visual_kinds_for_event(raw)),
            "created_at": text(safe.get("created_at")),
            "actor_label": actor_label,
            "type_label": type_label,
        }
        if round_no > 0:
            item["round"] = round_no
        projected.append(
            (
                item,
                {
                    "round": round_no,
                    "actor": keys.key("historyactor", actor_identity),
                    "type": keys.key("historytype", category),
                },
            )
        )
    return projected


def _history_filter_keys(
    keys: OpaqueKeyFactory,
    filters: Mapping[str, Any],
) -> OpaqueKeyFactory:
    serialized = json.dumps(
        dict(filters),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(serialized).hexdigest()[:24]
    return OpaqueKeyFactory(
        scope=f"{keys.scope}:history-filter:{digest}",
        secret=keys.secret,
    )


def _history_filter_options(
    projected: list[tuple[dict[str, Any], dict[str, Any]]],
) -> dict[str, list[dict[str, str]]]:
    rounds: dict[int, str] = {}
    actors: dict[str, str] = {}
    types: dict[str, str] = {}
    for item, meta in projected:
        round_no = to_int(meta.get("round"), 0) or 0
        if round_no > 0:
            rounds[round_no] = f"第 {round_no} 轮"
        actors[text(meta.get("actor"))] = text(item.get("actor_label"), "参与者")
        types[text(meta.get("type"))] = text(item.get("type_label"), "副本状态")
    return {
        "rounds": [
            {"value": str(value), "label": label}
            for value, label in sorted(rounds.items())
        ],
        "actors": [
            {"value": value, "label": label}
            for value, label in sorted(actors.items(), key=lambda entry: entry[1])
            if value
        ],
        "types": [
            {"value": value, "label": label}
            for value, label in sorted(types.items(), key=lambda entry: entry[1])
            if value
        ],
    }


def _validate_history_filters(
    values: Mapping[str, Any],
    options: Mapping[str, list[dict[str, str]]],
) -> dict[str, Any]:
    query = text(values.get("q"))[:120]
    raw_round = text(values.get("round"))
    round_no = 0
    if raw_round:
        round_no = to_int(raw_round, 0) or 0
        visible_rounds = {item["value"] for item in options.get("rounds", [])}
        if round_no <= 0 or str(round_no) not in visible_rounds:
            raise bad_request(
                "所选回合不在当前可见回放中。",
                recovery="请清空筛选或从当前回合选项中重新选择。",
            )
    actor = text(values.get("actor"))
    if actor and actor not in {item["value"] for item in options.get("actors", [])}:
        raise bad_request(
            "所选角色不在当前可见回放中。",
            recovery="请清空筛选或从当前角色选项中重新选择。",
        )
    event_type = text(values.get("type"))
    if event_type and event_type not in {item["value"] for item in options.get("types", [])}:
        raise bad_request(
            "所选事件类型不在当前可见回放中。",
            recovery="请清空筛选或从当前类型选项中重新选择。",
        )
    return {
        "q": query,
        "round": round_no,
        "actor": actor,
        "type": event_type,
    }


def _history_matches(
    item: Mapping[str, Any],
    meta: Mapping[str, Any],
    filters: Mapping[str, Any],
) -> bool:
    if filters.get("round") and int(meta.get("round") or 0) != int(filters["round"]):
        return False
    if filters.get("actor") and text(meta.get("actor")) != text(filters.get("actor")):
        return False
    if filters.get("type") and text(meta.get("type")) != text(filters.get("type")):
        return False
    needle = text(filters.get("q")).casefold()
    if needle:
        haystack = " ".join(
            text(item.get(field))
            for field in ("title", "summary", "actor_label", "type_label", "round")
        ).casefold()
        if needle not in haystack:
            return False
    return True


@_visual_route("session_summary")
async def session_summary_view(
    principal: Mapping[str, Any],
    database: Any,
    *,
    query: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    session, role, principal, keys = await _context(principal, database, query)
    conflict = _check_expected_revision(
        "session_summary", session, role, principal, query
    )
    if conflict:
        return conflict
    envelope = await build_session_summary(
        database,
        session,
        role=role,
        is_admin=bool(principal.get("is_admin")),
        keys=keys,
    )
    body = envelope.to_dict()
    data = mapping(body.get("data"))
    data["key"] = issue_surface_key(
        principal,
        "sessions",
        "session",
        text(session.get("id")),
    )
    data["object_kind"] = "session"
    fate_actions: list[dict[str, Any]] = []
    if role == "player" and callable(
        getattr(database, "list_actor_fate_previews", None)
    ):
        fate_envelope = await actor_fate_consent_view(
            principal,
            database,
            query={"session_id": text(session.get("id"))},
        )
        if int(fate_envelope.get("status") or 500) == 200:
            fate_data = mapping(mapping(fate_envelope.get("body")).get("data"))
            data["actor_fate_consent"] = fate_data
            fate_actions = [
                dict(item)
                for item in fate_data.get("available_actions") or ()
                if isinstance(item, Mapping)
            ]
            if fate_actions:
                data["configuration_actions"] = fate_actions
    permissions = mapping(body.get("permissions"))
    if (
        role in {"dm", "admin"}
        and bool(permissions.get("can_manage"))
        and not bool(body.get("readonly"))
        and not bool(data.get("session", {}).get("readonly"))
    ):
        revision = int(session.get("revision") or 0)
        configuration_actions = [
            dict(item)
            for item in data.get("available_actions") or ()
            if isinstance(item, Mapping)
        ]
        session_actions = [
            {
                "action_id": "C26",
                "intent": "session.pacing.preview",
                "label": "预览剧情节奏调整",
                "target_kind": "session",
                "expected_revision": revision,
                "description": "先生成不改动世界的预览；确认后建立快照并原子提交。",
                "transportReady": True,
                "focus_return": "opener",
                "fields": [
                    {
                        "name": "action",
                        "type": "select",
                        "labelKey": "action.field.pacing_action",
                        "required": True,
                        "options": [
                            {"value": "host_beat", "label": "主持推进一拍"},
                            {"value": "close_scene", "label": "结束当前场景"},
                            {"value": "skip_routine", "label": "跳过无风险过程"},
                            {"value": "transition", "label": "转入下一场景"},
                            {"value": "next_clue", "label": "开放下一条调查线索"},
                            {"value": "advance_chapter", "label": "推进到下一章节"},
                        ],
                    },
                    {
                        "name": "reason",
                        "type": "textarea",
                        "labelKey": "action.field.reason",
                        "required": True,
                    },
                ],
            },
            *_session_lifecycle_actions(
                session,
                roles={"admin"} if role == "admin" else {"host"},
            ),
        ]
        seen_actions: set[tuple[str, str]] = set()
        data["available_actions"] = []
        for action in [*configuration_actions, *session_actions]:
            identity = (text(action.get("action_id")), text(action.get("intent")))
            if identity in seen_actions:
                continue
            seen_actions.add(identity)
            data["available_actions"].append(action)
    elif role == "player" and fate_actions:
        # PageModel consumers read only ``available_actions``.  Keep the
        # configuration projection for the semantic session contract, while
        # exposing only this authenticated player's opaque fate descriptors to
        # the live console action controller.
        data["available_actions"] = list(fate_actions)
    else:
        data.pop("available_actions", None)
    body["data"] = data
    return {"status": 200, "body": body}


__all__ = [name for name in globals() if not name.startswith('__')]


