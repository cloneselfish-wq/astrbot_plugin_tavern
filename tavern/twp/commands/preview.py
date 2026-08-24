from .common import *
from .validation import *
from .scenes import *
from .entities import *
from .progression import *

def preview_command(
    world: Mapping[str, Any],
    state: Mapping[str, Any] | None,
    command: Mapping[str, Any],
) -> dict[str, Any]:
    """只读预览：不修改状态，返回派生影响清单供人工 DM 确认（§26.7）。"""
    try:
        result = apply_command(world, state, command, dry_run=True)
    except WorldCommandError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "summary": result["summary"],
        "events": result["events"],
        "changes": result["changes"],
        "affected": result["affected"],
        "revision_after": result["revision"],
    }


def list_commands() -> list[dict[str, Any]]:
    """命令目录，供 DM 操作台生成表单（§26.7）。"""
    catalog: list[dict[str, Any]] = []
    for domain in sorted(COMMAND_DOMAINS):
        for action in _DOMAIN_ACTIONS.get(domain, []):
            catalog.append(
                {
                    "domain": domain,
                    "action": action,
                    "label": _DOMAIN_ACTION_LABELS.get(f"{domain}.{action}", action),
                }
            )
    return catalog


__all__ = [name for name in globals() if not name.startswith('__')]

