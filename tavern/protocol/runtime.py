from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from .constants import RUNTIME_STATUSES, TWP_RUNTIME_SCHEMA, TWP_VERSION


META_FIELDS = frozenset(
    {
        "schema",
        "artifact_id",
        "revision",
        "event_sequence",
        "package_id",
        "content_version",
        "enabled_modules",
        "events",
        "modules",
    }
)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def is_twp_runtime(value: object) -> bool:
    return (
        isinstance(value, Mapping)
        and str(value.get("schema") or "").startswith("twp-runtime/")
        and isinstance(value.get("modules"), Mapping)
    )


def runtime_contract_from_world(
    world: Mapping[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    source = _mapping(world)
    raw = source.get("runtime_contract")
    if isinstance(raw, Mapping):
        return {
            str(module_id): _mapping(contract)
            for module_id, contract in raw.items()
            if str(module_id)
        }
    result: dict[str, dict[str, Any]] = {}
    for item in source.get("twp_modules") or []:
        if not isinstance(item, Mapping):
            continue
        module_id = str(item.get("module_id") or item.get("id") or "").strip()
        if not module_id:
            continue
        result[module_id] = {
            "schema": str(item.get("runtime_schema") or ""),
            "state_path": str(
                item.get("state_path") or f"runtime.modules.{module_id}"
            ),
            "state_fields": list(item.get("state_fields") or []),
            "absence_policy": str(
                item.get("absence_policy") or "not_applicable"
            ),
            "capabilities": list(item.get("capabilities") or []),
            "provider": _mapping(item.get("provider")),
            "required": bool(item.get("required")),
        }
    return result


def hydrate_runtime(
    flat: Mapping[str, Any],
    *,
    artifact_id: str = "",
    enabled_modules: list[str] | tuple[str, ...] | None = None,
    module_contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    source = deepcopy(dict(flat))
    contract = {
        str(module_id): _mapping(value)
        for module_id, value in _mapping(module_contract).items()
        if str(module_id)
    }
    explicit_module_set = (
        enabled_modules is not None
        or isinstance(source.get("enabled_modules"), (list, tuple, set))
    )
    raw_enabled = (
        enabled_modules
        if enabled_modules is not None
        else source.get("enabled_modules") or []
    )
    enabled = set(str(item) for item in raw_enabled)
    if not contract:
        contract = {
            module_id: {
                "state_path": f"runtime.modules.{module_id}",
                "state_fields": [],
                "absence_policy": "not_applicable",
            }
            for module_id in sorted(enabled)
        }
    root: dict[str, Any] = {
        "schema": TWP_RUNTIME_SCHEMA,
        "artifact_id": str(artifact_id or source.get("artifact_id") or ""),
        "revision": int(source.get("revision") or 0),
        "event_sequence": int(source.get("event_sequence") or 0),
        "package_id": str(source.get("package_id") or ""),
        "content_version": str(source.get("content_version") or ""),
        "enabled_modules": sorted(enabled),
        "events": list(source.get("events") or []),
        "modules": {},
    }
    consumed = set(META_FIELDS)
    explicit_states = _mapping(source.get("module_states"))
    consumed.add("module_states")
    for module_id, descriptor in contract.items():
        expected_path = f"runtime.modules.{module_id}"
        state_path = str(descriptor.get("state_path") or expected_path)
        if state_path != expected_path:
            raise ValueError(f"模块 {module_id} 的 state_path 越出自身命名空间")
        fields = tuple(
            str(item) for item in descriptor.get("state_fields") or []
        )
        state = {}
        for field in fields:
            if field in source:
                state[field] = deepcopy(source[field])
                consumed.add(field)
        if isinstance(explicit_states.get(module_id), Mapping):
            state.update(deepcopy(dict(explicit_states[module_id])))
        enabled_here = (
            module_id in enabled
            if explicit_module_set
            else bool(state) or bool(descriptor.get("required"))
        )
        if explicit_module_set and not enabled_here:
            state = {}
        status = "initialized" if enabled_here else "not_applicable"
        root["modules"][module_id] = {
            "status": status,
            "schema": str(
                descriptor.get("schema")
                or f"{module_id}.runtime/{TWP_VERSION}"
            ),
            "state": state,
        }
    extension = {
        key: deepcopy(value)
        for key, value in source.items()
        if key not in consumed
    }
    if extension and contract:
        raise ValueError(
            "运行态包含未被模块契约声明的字段："
            + "、".join(sorted(extension))
        )
    if extension:
        root["modules"]["extension_data"] = {
            "status": "initialized",
            "schema": f"extension-data.runtime/{TWP_VERSION}",
            "state": extension,
        }
    return root


def flatten_runtime(value: Mapping[str, Any] | None) -> dict[str, Any]:
    root = _mapping(value)
    if not is_twp_runtime(root):
        return deepcopy(root)
    flat: dict[str, Any] = {
        "artifact_id": str(root.get("artifact_id") or ""),
        "revision": int(root.get("revision") or 0),
        "event_sequence": int(root.get("event_sequence") or 0),
        "package_id": str(root.get("package_id") or ""),
        "content_version": str(root.get("content_version") or ""),
        "enabled_modules": list(root.get("enabled_modules") or []),
        "events": list(root.get("events") or []),
    }
    modules = _mapping(root.get("modules"))
    for module_id, payload in modules.items():
        module = _mapping(payload)
        status = str(module.get("status") or "corrupt")
        if status not in RUNTIME_STATUSES or status in {"disabled", "not_applicable"}:
            continue
        state = _mapping(module.get("state"))
        if module_id == "extension_data":
            flat.update(deepcopy(state))
            continue
        duplicated = set(flat) & set(state)
        if duplicated:
            raise ValueError(
                f"模块 {module_id} 与其他模块声明了重复运行态字段："
                + "、".join(sorted(duplicated))
            )
        flat.update(deepcopy(state))
    return flat


def runtime_from_state(state: Mapping[str, Any] | None) -> dict[str, Any]:
    value = _mapping(state)
    current = value.get("runtime")
    if isinstance(current, Mapping):
        return deepcopy(dict(current))
    return hydrate_runtime({})


def store_runtime(state: dict[str, Any], runtime: Mapping[str, Any]) -> None:
    state["runtime"] = deepcopy(dict(runtime))
