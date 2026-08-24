from .common import *
from .dashboard import *
from .session_dashboard import *
from .session_timeline import *

async def session_timeline(
    database: Any,
    session_id: str,
    limit: int = 30,
) -> dict[str, Any]:
    """事件时间线（回放视图）：事件流 + 操作摘要。"""
    limit = max(5, min(int(limit), 100))
    session = await database.get_session(session_id)
    session_name = _text(session.get("instance_name") or session.get("name"))
    events = await database.recent_events(session_id, limit)
    operations = await database.list_session_operations(session_id, limit)
    return {
        "session_id": session_id,
        "session_name": session_name,
        "events": [
            _event_projection(item, session_name)
            for item in events
            if isinstance(item, Mapping)
        ],
        "operations": [
            {
                "operation_id": _text(item.get("operation_id")),
                "operation_type": _text(item.get("operation_type")),
                "status": _text(item.get("status")),
                "created_at": _text(item.get("created_at")),
                "updated_at": _text(item.get("updated_at")),
                "source": _text(
                    item.get("request", {}).get("source")
                    if isinstance(item.get("request"), Mapping)
                    else ""
                ),
                "actor_id": _text(
                    item.get("request", {}).get("actor_id")
                    if isinstance(item.get("request"), Mapping)
                    else ""
                ),
            }
            for item in operations
            if isinstance(item, Mapping)
        ],
    }


__all__ = [name for name in globals() if not name.startswith('__')]

