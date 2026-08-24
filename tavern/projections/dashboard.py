from .common import *

def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bool(value: Any) -> bool:
    return bool(value)


def _display_world_time(value: Any) -> str:
    """Project the current in-world time without leaking a raw mapping."""
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, Mapping):
        return _text(value)
    for key in ("display", "label", "text", "current", "name"):
        text = _text(value.get(key))
        if text:
            return text
    parts = [
        _text(value.get(key))
        for key in (
            "era",
            "year",
            "season",
            "month",
            "day",
            "period",
            "hour",
        )
    ]
    return " ".join(part for part in parts if part)


def _status_entry(
    state: str,
    code: str = "",
    message: str = "",
) -> dict[str, Any]:
    """字段级读取状态：ready/empty/degraded/error。

    读取异常不再伪装成空数据——调用方必须同时返回旧字段（保持既有消费方
    兼容）与 ``data_status`` 里的状态条目，前端据此区分“模块未启用/无记录”
    （empty）和“数据库或投影异常”（error）。
    """
    entry: dict[str, Any] = {"state": state}
    if state in ("error", "degraded") and (code or message):
        entry["issue"] = {"code": code, "message": message}
    elif state == "empty" and message:
        entry["reason"] = message
    return entry


def _projection_world(
    snapshot: Mapping[str, Any],
    current: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """补齐旧副本快照缺失的展示索引，同时保持冻结运行规则不变。

    之前创建的副本可能没有 ``entity_index``、冻结文本目录或模块
    definitions。运行状态仍以副本快照为准；这里只从同 slug 的当前世界补入
    名称、说明和状态字典，避免 WebUI 把内部引用显示成“解析失败”。
    """

    merged = dict(snapshot or {})
    latest = dict(current or {})
    if not latest or str(latest.get("slug") or "") != str(
        merged.get("slug") or ""
    ):
        return merged
    for key in (
        "entity_index",
        "resolved_text_catalog",
        "localization_metadata",
    ):
        if not merged.get(key) and latest.get(key):
            merged[key] = latest[key]
    frozen_rules = dict(merged.get("rules") or {})
    latest_rules = dict(latest.get("rules") or {})
    for module_id, collections in {
        "quest_graph": ("quests", "states", "statuses", "status_definitions"),
        "faction_state": ("factions", "states", "stances", "status_definitions"),
        "npc_lifecycle": (
            "npcs",
            "states",
            "lifecycle_states",
            "conditions",
        ),
        "npc_presence": ("states", "statuses", "status_definitions"),
        "npc_condition": ("states", "conditions", "status_definitions"),
    }.items():
        block = dict(frozen_rules.get(module_id) or {})
        latest_block = dict(latest_rules.get(module_id) or {})
        for collection in collections:
            if not block.get(collection) and latest_block.get(collection):
                block[collection] = latest_block[collection]
        if block:
            frozen_rules[module_id] = block
    merged["rules"] = frozen_rules
    return merged


def _event_projection(item: Mapping[str, Any], session_name: str) -> dict[str, Any]:
    meta = item.get("meta") if isinstance(item.get("meta"), Mapping) else {}
    actor_name = _text(item.get("actor_name"))
    role = _text(item.get("role"))
    return {
        "id": _text(item.get("id")),
        "seq": _int(item.get("seq")),
        "session_id": _text(item.get("session_id")),
        "session_name": session_name,
        "turn_no": _int(item.get("turn_no")),
        "role": role,
        "event_type": _text(meta.get("event_type") or meta.get("type") or meta.get("kind") or role),
        "actor_id": _text(item.get("actor_id")),
        "player_name": _text(meta.get("player_name") or actor_name),
        "character_name": _text(meta.get("character_name") or meta.get("actor_character") or actor_name),
        "operation_source": _text(meta.get("operation_source") or meta.get("source") or meta.get("transport") or role),
        "content": _text(item.get("content"))[:600],
        "created_at": _text(item.get("created_at")),
    }


def _options(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, Mapping):
        items = payload.get("options") or payload.get("choices") or []
    else:
        return []
    result: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, Mapping):
            result.append(
                {
                    "key": _text(item.get("key"), _text(item.get("id"))),
                    "text": _text(item.get("text"), _text(item.get("label"))),
                    "risk": _text(item.get("risk")),
                    "collective": _bool(item.get("collective")),
                    "requires_check": _bool(
                        item.get("requires_check") or item.get("check")
                    ),
                }
            )
    return result


def _normalize_timer(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": _text(item.get("id")),
        "timer_type": _text(item.get("timer_type")),
        "status": _text(item.get("status"), "active"),
        "participant": _text(
            item.get("character_name") or item.get("display_name")
        ),
        "deadline_at": _text(item.get("deadline_at")),
        "remaining_seconds": _int(item.get("remaining_seconds"), -1),
        "reminder_sent": _bool(item.get("reminder_sent")),
        "created_at": _text(item.get("created_at")),
    }


def _timer_sort_key(timer: Mapping[str, Any]) -> str:
    """倒序依据：优先 created_at（最新创建置顶），缺失时退化为 deadline。"""
    return _text(timer.get("created_at")) or _text(timer.get("deadline_at")) or ""


async def session_timers(
    database: Any,
    session_id: str,
    order: str = "desc",
) -> list[dict[str, Any]]:
    """副本的倒计时列表（0.12.0-A3，#3）。

    - 默认 ``desc``：最新内容置顶（created_at 倒序）；
    - ``asc``：最紧迫的（deadline 最早）置顶，适合倒计时场景；
    - 只返回活跃/暂停中的计时器，已结束的不出现在小窗口。
    """
    timers = await database.list_timers(session_id)
    normalized = [_normalize_timer(item) for item in timers]
    normalized = [
        item for item in normalized if item["status"] in {"active", "paused"}
    ]
    normalized.sort(
        key=_timer_sort_key,
        reverse=(str(order).strip().lower() != "asc"),
    )
    return normalized


def _normalize_vote(vote: Any) -> dict[str, Any] | None:
    if not vote or not isinstance(vote, Mapping):
        return None
    ballots = vote.get("ballots") or vote.get("votes") or []
    tally: dict[str, int] = {}
    ballot_rows: list[dict[str, str]] = []
    voted_ids: set[str] = set()
    for ballot in ballots if isinstance(ballots, list) else []:
        if not isinstance(ballot, Mapping):
            continue
        bid = _text(ballot.get("user_id"))
        bkey = _text(ballot.get("choice_key") or ballot.get("key"))
        if bid:
            voted_ids.add(bid)
        if bkey:
            tally[bkey] = tally.get(bkey, 0) + 1
        ballot_rows.append({"user_id": bid, "choice_key": bkey})
    eligible = [_text(x) for x in vote.get("eligible_user_ids") or []]
    unvoted = [u for u in eligible if u and u not in voted_ids]
    options = _options(vote.get("options"))
    for option in options:
        option["votes"] = tally.get(option["key"], 0)
    remaining_seconds = 0
    if vote.get("deadline_at"):
        try:
            from datetime import datetime, timezone as _tz

            raw = str(vote["deadline_at"]).replace("Z", "+00:00")
            deadline = datetime.fromisoformat(raw)
            remaining_seconds = max(
                0, int((deadline - datetime.now(_tz.utc)).total_seconds())
            )
        except Exception:
            remaining_seconds = 0
    return {
        "id": _text(vote.get("id")),
        "title": _text(vote.get("title"), _text(vote.get("topic"))),
        "status": _text(vote.get("status"), "open"),
        "winner_key": _text(vote.get("winner_key")),
        "deadline_at": _text(vote.get("deadline_at")),
        "remaining_seconds": remaining_seconds,
        "majority": _int(vote.get("majority"), 0),
        "voters": _int(vote.get("voter_count") or len(ballots), 0),
        "eligible_user_ids": eligible,
        "unvoted_user_ids": unvoted,
        "ballots": ballot_rows,
        "options": options,
    }


async def dashboard_sessions(database: Any) -> list[dict[str, Any]]:
    """副本概览列表：状态、世界、当前行动者与活跃计时器数量。

    v1.0-A2（性能优化）：活跃计时器计数直接使用 ``list_sessions``
    已聚合的 ``active_timer_count`` 列，不再对每个副本单独查询计时器。
    D1：已归档副本显式标记 ``readonly`` / ``archived``，前端据此进入
    只读视图，不再把归档副本当作可操作副本。
    """
    sessions = await database.list_sessions()
    archived_ids: set[str] = set()
    try:
        archive_rows = await database.execute_read(
            "SELECT session_id FROM session_archives",
            (),
        )
        archived_ids = {
            str(row.get("session_id"))
            for row in archive_rows or ()
            if isinstance(row, Mapping)
        }
    except Exception:
        archived_ids = set()
    actor_names: Mapping[str, Any] = {}
    actor_names_error = False
    session_ids = [
        _text(session.get("id"))
        for session in sessions
        if isinstance(session, Mapping) and _text(session.get("id"))
    ]
    try:
        raw_actor_names = await database.list_turn_actor_names(session_ids)
        if isinstance(raw_actor_names, Mapping):
            actor_names = raw_actor_names
    except Exception:
        actor_names_error = True
    result: list[dict[str, Any]] = []
    for session in sessions:
        session_id = _text(session.get("id"))
        if not session_id:
            continue
        state = _text(session.get("state"), "closed")
        turn: Mapping[str, Any] = {}
        turn_status = _status_entry("ready")
        try:
            if actor_names_error:
                raise RuntimeError("turn actor directory unavailable")
            stored_turn = session.get("turn_state")
            turn = (
                dict(stored_turn)
                if isinstance(stored_turn, Mapping)
                else {}
            )
            names = actor_names.get(session_id)
            names = names if isinstance(names, Mapping) else {}
            current_ref = _text(turn.get("current_user_id"))
            turn["current_name"] = _text(names.get(current_ref))
        except Exception:
            turn = {}
            turn_status = _status_entry(
                "error",
                "dashboard.turn_status.read_failed",
                "当前行动状态读取失败",
            )
        result.append(
            {
                "id": session_id,
                "name": _text(
                    session.get("instance_name") or session.get("name")
                ),
                "world": _text(
                    session.get("world_name") or session.get("world_slug")
                ),
                "state": state,
                "round_no": _int(
                    turn.get("round_no"), _int(session.get("round_no"))
                ),
                "current_name": _text(turn.get("current_name")),
                "active_timers": _int(session.get("active_timer_count")),
                "selected": _bool(session.get("selected")),
                "readonly": state == "finished",
                "archived": session_id in archived_ids,
                "turn_status": turn_status,
            }
        )
    # 正在运行的排前面，其余按更新时间倒序。
    result.sort(
        key=lambda item: (
            item["state"] != SESSION_RUNNING,
            -item["round_no"],
        )
    )
    return result


async def _waiting_for(
    database: Any,
    session_id: str,
    state: str,
) -> str:
    """副本当前等待流程（vote / choice / preparation / admin），0.12.0-A3。"""
    if state == "closed":
        return ""
    row = await database.execute_read(
        """
        SELECT
            EXISTS(
                SELECT 1 FROM group_votes
                WHERE session_id = ? AND status = 'open'
            ) AS has_vote,
            EXISTS(
                SELECT 1 FROM choice_sets
                WHERE session_id = ? AND status = 'active'
            ) AS has_choice
        """,
        (session_id, session_id),
    )
    item = row[0] if row else {}
    if bool(item.get("has_vote")):
        return "vote"
    if bool(item.get("has_choice")):
        return "choice"
    if state == "preparing":
        return "preparation"
    if state == "paused":
        return "admin"
    return ""


def _world_labels(instance: Mapping[str, Any]) -> dict[str, str]:
    """从冻结世界快照构建 TWP 模块 ID → 中文标签映射。

    覆盖场景、任务、NPC、阵营、配方、成长轨迹、遭遇模板与知识事实。
    标签字段以各模块定义（label / name / text）为权威；缺失时留空，
    由前端按“原始 ID + 友好兜底”降级显示，不伪造字段含义。
    同时登记去掉命名空间前缀的短键，便于前端后缀匹配。
    """
    labels: dict[str, str] = {}
    snapshot = (
        instance.get("world_snapshot")
        if isinstance(instance, Mapping)
        else None
    )
    if not isinstance(snapshot, Mapping):
        return labels
    rules = snapshot.get("rules")
    if not isinstance(rules, Mapping):
        return labels

    def add(prefix: str, key: Any, value: Any) -> None:
        key = _text(key)
        value = _text(value)
        if key and value:
            labels[key] = value
            marker = prefix + ":"
            if key.startswith(marker) and len(key) > len(marker):
                labels.setdefault(key[len(marker):], value)

    def items(module: str, field: str) -> list[Mapping[str, Any]]:
        block = rules.get(module)
        if not isinstance(block, Mapping):
            return []
        values = block.get(field)
        if not isinstance(values, list):
            return []
        return [item for item in values if isinstance(item, Mapping)]

    for item in items("scene_graph", "nodes"):
        add("scene", item.get("id"), item.get("label"))
    for item in items("quest_graph", "quests"):
        add("quest", item.get("id"), item.get("label") or item.get("name"))
    for item in items("npc_lifecycle", "npcs"):
        add("npc", item.get("id"), item.get("name") or item.get("label"))
    for item in items("faction_state", "factions"):
        add("faction", item.get("id"), item.get("name") or item.get("label"))
    for item in items("crafting", "recipes"):
        add("recipe", item.get("id"), item.get("label"))
    for item in items("progression", "tracks"):
        add("track", item.get("id"), item.get("label"))
    for item in items("challenge_engine", "templates"):
        add("challenge", item.get("id"), item.get("label"))
    for item in items("knowledge_graph", "facts"):
        add("fact", item.get("id"), item.get("text"))
    return labels


def _world_display_label(
    value: Any,
    labels: Mapping[str, Any],
) -> str:
    """Resolve one world reference without leaking a stable/internal identifier.

    World state may contain either a human-authored label or a stable TWP
    reference such as ``scene:vault_audit_hall``.  The list and overview DTOs
    must never guess a label from the identifier.  They use the frozen world
    snapshot first, keep genuinely human-readable values, and otherwise return
    an empty label so the UI can report that the display name is unavailable.
    """

    ref = _text(value)
    if not ref:
        return ""
    candidates = [ref]
    if ":" in ref:
        candidates.append(ref.split(":", 1)[1])
    for candidate in candidates:
        resolved = _text(labels.get(candidate))
        if resolved:
            return resolved
    if any("\u3400" <= char <= "\u9fff" for char in ref):
        return ref
    if not _INTERNAL_DISPLAY_REF.fullmatch(ref):
        return ref
    return ""


__all__ = [name for name in globals() if not name.startswith('__')]

