"""Domain repository methods extracted from the SQLite store."""

from contextlib import nullcontext

from ..database_support import *
from .events import append_event


def _actor_principal_ref(actor_id: object) -> str:
    digest = hashlib.sha256(
        str(actor_id or "").encode("utf-8")
    ).hexdigest()[:12].upper()
    return f"public:actor:{digest}"



__all__ = [name for name in globals() if not name.startswith('__')]
