"""Domain repository methods extracted from the SQLite store."""

from sqlite3 import Connection

from ..database_support import *
from ..entity_registry import EntityRegistry
from ..errors import DataIntegrityError
from ..resolution_receipts import content_hash
from ..rule_runtime import enabled_feature_versions


class BuiltinWorldConflictError(DataIntegrityError):
    """Stable, machine-readable rejection for built-in identity collisions."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = str(code)


def _world_write_operation_id(world_id: str, idempotency_key: str) -> str:
    digest = content_hash(
        {
            "scope": "world.write",
            "world_id": str(world_id),
            "idempotency_key": str(idempotency_key),
        }
    )
    return f"console-world-write:{digest[:40]}"


def _character_write_operation_id(
    world_id: str,
    actor_id: str,
    idempotency_key: str,
) -> str:
    digest = content_hash(
        {
            "scope": "character.write",
            "world_id": str(world_id),
            "actor_id": str(actor_id),
            "idempotency_key": str(idempotency_key),
        }
    )
    return f"console-character-write:{digest[:40]}"


def _insert_world_event(
    connection: Connection,
    *,
    event_id: str,
    session_id: str,
    turn_no: int,
    actor_id: str,
    actor_name: str,
    content: str,
    meta: Mapping[str, Any],
    created_at: str,
) -> None:
    """世界模块唯一的运行事件写入器；调用方必须处于既有事务中。"""

    connection.execute(
        """
        INSERT INTO events(
            id, session_id, turn_no, role, actor_id,
            actor_name, content, meta_json, created_at
        ) VALUES (?, ?, ?, 'system', ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            session_id,
            turn_no,
            actor_id,
            actor_name,
            content,
            json_dump(dict(meta)),
            created_at,
        ),
    )



__all__ = [name for name in globals() if not name.startswith('__')]
