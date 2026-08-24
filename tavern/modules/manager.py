from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from pathlib import Path

from .catalog import PLUGIN_MODULES, PluginModuleSpec


class ModuleDependencyError(ValueError):
    """A module transition would violate the dependency graph."""


@dataclass(slots=True)
class _RuntimeState:
    enabled: bool
    status: str = "ready"
    message: str = ""
    changed_at: str = ""


class PluginModuleManager:
    """Concurrency-safe capability and lifecycle registry for plugin domains."""

    def __init__(
        self,
        specs: Iterable[PluginModuleSpec] = PLUGIN_MODULES,
        state_path: Path | None = None,
    ) -> None:
        source = tuple(specs)
        self._specs = {item.id: item for item in source}
        if len(self._specs) != len(source):
            raise ModuleDependencyError("插件模块 ID 不能重复")
        self._validate_graph()
        self._state_path = Path(state_path) if state_path is not None else None
        now = datetime.now(timezone.utc).isoformat()
        self._states = {
            item.id: _RuntimeState(True, changed_at=now)
            for item in self._specs.values()
        }
        self._lock = asyncio.Lock()
        self._load_state()

    def _load_state(self) -> None:
        if self._state_path is None or not self._state_path.is_file():
            return
        try:
            value = json.loads(self._state_path.read_text(encoding="utf-8"))
            enabled = value.get("enabled", {}) if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(enabled, dict):
            return
        for module_id, raw in enabled.items():
            if module_id in self._states and not self._specs[module_id].required:
                self._states[module_id].enabled = bool(raw)
                self._states[module_id].status = "ready" if raw else "disabled"
        # Repair externally edited/corrupt state by re-enabling dependencies.
        changed = True
        while changed:
            changed = False
            for item in self._specs.values():
                if not self.enabled(item.id):
                    continue
                for dependency in item.dependencies:
                    if not self.enabled(dependency):
                        self._states[dependency].enabled = True
                        self._states[dependency].status = "ready"
                        changed = True

    def _write_state(self) -> None:
        if self._state_path is None:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(
            prefix=".plugin-modules-",
            suffix=".json",
            dir=self._state_path.parent,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(
                    {"format": 1, "enabled": {key: state.enabled for key, state in self._states.items()}},
                    handle,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self._state_path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def _validate_graph(self) -> None:
        unknown = {
            dependency
            for item in self._specs.values()
            for dependency in item.dependencies
            if dependency not in self._specs
        }
        if unknown:
            raise ModuleDependencyError(
                "插件模块存在未知依赖：" + "、".join(sorted(unknown))
            )
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(module_id: str) -> None:
            if module_id in visiting:
                raise ModuleDependencyError(f"插件模块依赖成环：{module_id}")
            if module_id in visited:
                return
            visiting.add(module_id)
            for dependency in self._specs[module_id].dependencies:
                visit(dependency)
            visiting.remove(module_id)
            visited.add(module_id)

        for module_id in self._specs:
            visit(module_id)

    def enabled(self, module_id: str) -> bool:
        state = self._states.get(str(module_id))
        return bool(state and state.enabled)

    def require(self, module_id: str) -> None:
        if not self.enabled(module_id):
            raise ModuleDependencyError(f"插件模块 {module_id} 当前未启用")

    def capabilities(self) -> dict[str, str]:
        return {
            capability: item.id
            for item in self._specs.values()
            if self.enabled(item.id)
            for capability in item.capabilities
        }

    def catalog(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for item in self._specs.values():
            state = self._states[item.id]
            consumers = sorted(
                candidate.id
                for candidate in self._specs.values()
                if item.id in candidate.dependencies
            )
            result.append(
                {
                    **item.export(),
                    "enabled": state.enabled,
                    "status": state.status,
                    "message": state.message,
                    "changed_at": state.changed_at,
                    "consumers": consumers,
                    "can_disable": not item.required and not any(
                        self.enabled(candidate) for candidate in consumers
                    ),
                }
            )
        return result

    async def set_enabled(self, module_id: str, enabled: bool) -> dict[str, Any]:
        module_id = str(module_id or "").strip()
        if module_id not in self._specs:
            raise ModuleDependencyError(f"未知插件模块：{module_id}")
        async with self._lock:
            spec = self._specs[module_id]
            if not enabled and spec.required:
                raise ModuleDependencyError(f"核心模块 {spec.label} 不能停用")
            if enabled:
                missing = [item for item in spec.dependencies if not self.enabled(item)]
                if missing:
                    raise ModuleDependencyError(
                        "请先启用依赖：" + "、".join(missing)
                    )
            else:
                consumers = [
                    item.id
                    for item in self._specs.values()
                    if module_id in item.dependencies and self.enabled(item.id)
                ]
                if consumers:
                    raise ModuleDependencyError(
                        "仍被这些模块依赖：" + "、".join(consumers)
                    )
            state = self._states[module_id]
            state.enabled = bool(enabled)
            state.status = "ready" if enabled else "disabled"
            state.message = "" if enabled else "由管理员停用"
            state.changed_at = datetime.now(timezone.utc).isoformat()
            self._write_state()
            return next(item for item in self.catalog() if item["id"] == module_id)

    async def report_health(
        self,
        module_id: str,
        *,
        status: str,
        message: str = "",
    ) -> None:
        if module_id not in self._states:
            return
        async with self._lock:
            state = self._states[module_id]
            state.status = str(status or "unknown")
            state.message = str(message or "")[:500]
            state.changed_at = datetime.now(timezone.utc).isoformat()


__all__ = ["ModuleDependencyError", "PluginModuleManager"]
