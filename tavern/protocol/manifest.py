from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from typing import Any

from ..constants import PLUGIN_VERSION
from .constants import (
    TWP_COMPILER_ABI,
    TWP_CORE_VERSION,
    TWP_FORMAT,
    TWP_MATURITY,
    TWP_NAME,
    TWP_PACKAGE_FORMAT,
    TWP_MODULE_API_VERSION,
    TWP_VERSION,
)
from .errors import TwpPackageError, TwpValidationIssue
from .models import ModuleDescriptor


def _error(code: str, message: str, path: str = "", hint: str = "") -> TwpPackageError:
    return TwpPackageError(TwpValidationIssue(code, message, path, "error", hint))


_PRERELEASE_PART = re.compile(r"^[0-9a-z]+$")
_SUBPROJECT_VERSION_CEILING = (1, 0, 0)


def _version_key(
    value: object,
) -> tuple[int, int, int, int, tuple[tuple[int, int | str], ...]] | None:
    """Return a SemVer-compatible key for supported plugin versions.

    Prerelease identifiers are compared individually: numeric identifiers
    compare numerically and a release without a suffix sorts after every
    prerelease of the same core version.
    """

    text = str(value or "").strip().lower().removeprefix("v")
    head, _, suffix = text.partition("-")
    parts = head.split(".")
    if not 1 <= len(parts) <= 3 or any(not part.isdigit() for part in parts):
        return None
    major, minor, patch = (int(item) for item in (parts + ["0", "0"])[:3])
    if not suffix:
        return major, minor, patch, 1, ()
    identifiers = tuple(
        part for part in re.split(r"[._-]", suffix) if part
    )
    if not identifiers or any(
        _PRERELEASE_PART.fullmatch(part) is None for part in identifiers
    ):
        return None
    prerelease = tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in identifiers
    )
    return major, minor, patch, 0, prerelease


def _identifier(value: object, path: str) -> str:
    text = str(value or "").strip()
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789._-")
    if (
        not text
        or len(text) > 160
        or not text[0].isalnum()
        or any(char not in allowed for char in text)
    ):
        raise _error("protocol.manifest_invalid", f"{path} 必须是小写稳定标识", path)
    return text


def _exact_subproject_version(value: object, path: str) -> str:
    text = str(value or "").strip()
    key = _version_key(text)
    if key is None:
        raise _error("protocol.manifest_invalid", f"{path} 不是有效 SemVer", path)
    if key[:3] > _SUBPROJECT_VERSION_CEILING:
        raise _error(
            "protocol.unsupported",
            f"{path} 不得超过 1.0.0",
            path,
        )
    return text


def validate_manifest(value: Mapping[str, Any]) -> dict[str, Any]:
    manifest = dict(value)
    if manifest.get("format") != TWP_FORMAT:
        raise _error(
            "protocol.manifest_invalid",
            f"format 必须为 {TWP_FORMAT}",
            "tavern-world.json.format",
        )
    if int(manifest.get("package_format") or 0) != TWP_PACKAGE_FORMAT:
        raise _error(
            "protocol.unsupported",
            f"当前版本只接受 TWP package_format={TWP_PACKAGE_FORMAT}",
            "tavern-world.json.package_format",
        )
    protocol = manifest.get("protocol")
    if not isinstance(protocol, Mapping):
        raise _error(
            "protocol.manifest_invalid",
            "manifest.protocol 必须是对象",
            "tavern-world.json.protocol",
        )
    if str(protocol.get("name") or "") != TWP_NAME:
        raise _error("protocol.unsupported", "protocol.name 必须为 twp", "protocol.name")
    if str(protocol.get("core") or "") != TWP_CORE_VERSION:
        raise _error(
            "protocol.unsupported",
            f"当前版本支持 TWP core={TWP_CORE_VERSION}",
            "protocol.core",
        )
    if str(protocol.get("compiler_abi") or "") != TWP_COMPILER_ABI:
        raise _error(
            "protocol.unsupported",
            f"当前版本支持 compiler_abi={TWP_COMPILER_ABI}",
            "protocol.compiler_abi",
        )
    if str(protocol.get("maturity") or "") != TWP_MATURITY:
        raise _error(
            "protocol.manifest_invalid",
            f"当前版本要求 manifest maturity={TWP_MATURITY}",
            "protocol.maturity",
        )
    if str(protocol.get("version") or "") != TWP_VERSION:
        raise _error(
            "protocol.unsupported",
            f"当前版本只接受 TWP version={TWP_VERSION}",
            "protocol.version",
        )

    identity = manifest.get("identity")
    if not isinstance(identity, Mapping):
        raise _error(
            "protocol.manifest_invalid",
            "manifest.identity 必须是对象",
            "tavern-world.json.identity",
        )
    package_id = _identifier(identity.get("package_id"), "identity.package_id")
    namespace = _identifier(identity.get("namespace"), "identity.namespace")
    name = str(identity.get("name") or "").strip()
    content_version = _exact_subproject_version(
        identity.get("content_version"), "identity.content_version"
    )
    # 内容可以在同一 RC9 协议下递增修订（例如 rc9.1），以便内嵌世界
    # 修复后能被已安装的 rc9 旧内容识别为新版本。协议、core、compiler
    # ABI 和 module API 仍必须精确等于 RC9；这里不会放宽到 RC8 或 RC10。
    if not (
        content_version == TWP_VERSION
        or content_version.startswith(f"{TWP_VERSION}.")
    ):
        raise _error(
            "protocol.unsupported",
            f"当前世界包 content_version 必须属于 {TWP_VERSION} 修订系列",
            "identity.content_version",
        )
    if not name or not content_version:
        raise _error(
            "protocol.manifest_invalid",
            "identity.name 和 identity.content_version 不能为空",
            "identity",
        )

    minimum = _version_key(manifest.get("minimum_plugin_version"))
    current = _version_key(PLUGIN_VERSION)
    if minimum is None:
        raise _error(
            "protocol.manifest_invalid",
            "minimum_plugin_version 格式无效",
            "minimum_plugin_version",
        )
    if current is not None and minimum > current:
        raise _error(
            "protocol.unsupported",
            f"世界包要求插件 {manifest['minimum_plugin_version']}，当前为 {PLUGIN_VERSION}",
            "minimum_plugin_version",
        )

    world = manifest.get("world")
    if not isinstance(world, Mapping) or not str(world.get("entry") or "").strip():
        raise _error(
            "protocol.manifest_invalid",
            "world.entry 必须指向世界核心 JSON",
            "world.entry",
        )
    modules = manifest.get("modules")
    if not isinstance(modules, Sequence) or isinstance(modules, (str, bytes)):
        raise _error(
            "protocol.manifest_invalid",
            "manifest.modules 必须是数组",
            "modules",
        )
    normalized = {
        **manifest,
        "identity": {
            **dict(identity),
            "package_id": package_id,
            "namespace": namespace,
            "name": name,
            "content_version": content_version,
        },
        "protocol": dict(protocol),
        "world": dict(world),
        "modules": [dict(item) for item in modules if isinstance(item, Mapping)],
    }
    for index, module in enumerate(normalized["modules"]):
        api_version = _exact_subproject_version(
            module.get("api_version"), f"modules[{index}].api_version"
        )
        if api_version != TWP_MODULE_API_VERSION:
            raise _error(
                "protocol.unsupported",
                f"当前模块 API 必须为 {TWP_MODULE_API_VERSION}",
                f"modules[{index}].api_version",
            )
    return normalized


def parse_module_descriptor(value: Mapping[str, Any], *, path: str) -> ModuleDescriptor:
    module_id = _identifier(value.get("module_id"), f"{path}.module_id")
    depends_on = value.get("depends_on") or []
    if not isinstance(depends_on, Sequence) or isinstance(depends_on, (str, bytes)):
        raise _error(
            "protocol.manifest_invalid",
            f"{module_id}.depends_on 必须是数组",
            f"{path}.depends_on",
        )
    entities = value.get("entity_collections") or []
    if not isinstance(entities, Sequence) or isinstance(entities, (str, bytes)):
        raise _error(
            "protocol.manifest_invalid",
            f"{module_id}.entity_collections 必须是数组",
            f"{path}.entity_collections",
        )
    provider = value.get("provider")
    provider = dict(provider) if isinstance(provider, Mapping) else {}
    runtime = value.get("runtime")
    runtime = dict(runtime) if isinstance(runtime, Mapping) else {}
    state_fields = runtime.get("state_fields") or value.get("state_fields") or []
    capabilities = value.get("capabilities") or []
    text_collections = value.get("text_collections") or []
    for label, items in (
        ("runtime.state_fields", state_fields),
        ("capabilities", capabilities),
        ("text_collections", text_collections),
    ):
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
            raise _error(
                "protocol.manifest_invalid",
                f"{module_id}.{label} 必须是数组",
                f"{path}.{label}",
            )
    state_path = str(
        runtime.get("state_path")
        or value.get("state_path")
        or f"runtime.modules.{module_id}"
    ).strip()
    expected_state_path = f"runtime.modules.{module_id}"
    if state_path != expected_state_path:
        raise _error(
            "protocol.manifest_invalid",
            f"{module_id}.runtime.state_path 必须位于模块自己的命名空间",
            f"{path}.runtime.state_path",
        )
    normalized_text_collections: list[dict[str, Any]] = []
    seen_text_paths: set[tuple[str, str]] = set()
    for index, raw_collection in enumerate(text_collections):
        collection_path = f"{path}.text_collections[{index}]"
        if not isinstance(raw_collection, Mapping):
            raise _error(
                "protocol.manifest_invalid",
                f"{module_id}.text_collections 每项必须是对象",
                collection_path,
            )
        item = dict(raw_collection)
        root = str(item.get("path") or "$").strip()
        segments = root[2:].split(".") if root.startswith("$.") else []
        if root != "$" and (
            not segments
            or any(
                not segment
                or any(
                    not (char.isalnum() or char in {"_", "-"})
                    for char in segment
                )
                for segment in segments
            )
        ):
            raise _error(
                "protocol.manifest_invalid",
                f"{module_id}.text_collections.path 必须是模块内的 $ 路径",
                f"{collection_path}.path",
            )
        strategy = str(item.get("strategy") or "visible_text_fields")
        if strategy != "visible_text_fields":
            raise _error(
                "protocol.manifest_invalid",
                f"{module_id}.text_collections.strategy 不受支持：{strategy}",
                f"{collection_path}.strategy",
            )
        audience = str(item.get("audience") or "player")
        if audience not in {"player", "dm", "author"}:
            raise _error(
                "protocol.manifest_invalid",
                f"{module_id}.text_collections.audience 无效：{audience}",
                f"{collection_path}.audience",
            )
        array_ids = str(item.get("array_id_strategy") or "stable_id")
        if array_ids not in {"stable_id", "index"}:
            raise _error(
                "protocol.manifest_invalid",
                f"{module_id}.text_collections.array_id_strategy 无效：{array_ids}",
                f"{collection_path}.array_id_strategy",
            )
        identity = (root, audience)
        if identity in seen_text_paths:
            raise _error(
                "protocol.manifest_invalid",
                f"{module_id}.text_collections 重复声明：{root}",
                collection_path,
            )
        seen_text_paths.add(identity)
        normalized_text_collections.append(
            {
                **item,
                "path": root,
                "strategy": strategy,
                "audience": audience,
                "array_id_strategy": array_ids,
            }
        )
    api_version = _exact_subproject_version(
        value.get("api_version") or TWP_MODULE_API_VERSION,
        f"{path}.api_version",
    )
    if api_version != TWP_MODULE_API_VERSION:
        raise _error(
            "protocol.unsupported",
            f"当前模块描述符 API 必须为 {TWP_MODULE_API_VERSION}",
            f"{path}.api_version",
        )
    return ModuleDescriptor(
        module_id=module_id,
        api_version=api_version,
        definition_schema=str(value.get("definition_schema") or ""),
        runtime_schema=str(value.get("runtime_schema") or ""),
        definitions=str(value.get("definitions") or ""),
        commands=str(value.get("commands") or ""),
        events=str(value.get("events") or ""),
        projections=str(value.get("projections") or ""),
        migration_dir=str(value.get("migration_dir") or "migrations/"),
        tests_dir=str(value.get("tests_dir") or "tests/"),
        depends_on=tuple(str(item) for item in depends_on),
        read_paths=tuple(str(item) for item in value.get("read_paths") or []),
        write_paths=tuple(str(item) for item in value.get("write_paths") or []),
        state_path=state_path,
        state_fields=tuple(str(item) for item in state_fields),
        capabilities=tuple(str(item) for item in capabilities),
        text_collections=tuple(normalized_text_collections),
        absence_policy=str(value.get("absence_policy") or "not_applicable"),
        provider_kind=str(provider.get("kind") or "builtin"),
        provider_id=str(provider.get("id") or module_id),
        required=bool(value.get("required")),
        enabled=bool(value.get("enabled", True)),
        entity_collections=tuple(dict(item) for item in entities if isinstance(item, Mapping)),
    )
