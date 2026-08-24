from .visual_support import *
from .session_summary import *
from .session_party import *
from .session_world import *

@_visual_route("generation")
async def session_generation_view(
    principal: Mapping[str, Any],
    database: Any,
    *,
    query: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    session, role, principal, keys = await _context(principal, database, query)
    conflict = _check_expected_revision(
        "generation", session, role, principal, query
    )
    if conflict:
        return conflict
    values = mapping(query)
    page_size = max(1, min(50, to_int(values.get("page_size"), 10) or 10))
    return _response(
        await build_session_generation(
            database,
            session,
            role=role,
            is_admin=bool(principal.get("is_admin")),
            keys=keys,
            cursor=text(values.get("cursor")),
            page_size=page_size,
        )
    )


__all__ = [name for name in globals() if not name.startswith('__')]


