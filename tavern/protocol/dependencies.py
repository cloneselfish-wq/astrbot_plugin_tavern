from .common import *
from .archive_reader import *

import unicodedata

def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_path(info: zipfile.ZipInfo) -> PurePosixPath:
    original = str(getattr(info, "orig_filename", info.filename)).rstrip("/")
    name = info.filename.rstrip("/")
    if not original or "\\" in original or not name:
        raise _issue("protocol.manifest_invalid", "ZIP 包含非法路径", info.filename)
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise _issue("protocol.manifest_invalid", "ZIP 包含越界路径", info.filename)
    mode = (info.external_attr >> 16) & 0xFFFF
    if mode and stat.S_ISLNK(mode):
        raise _issue("protocol.manifest_invalid", "TWP 包不允许符号链接", info.filename)
    if info.flag_bits & 0x1:
        raise _issue("protocol.manifest_invalid", "TWP 包不支持加密成员", info.filename)
    return path


def _safe_archive_names(infos: Sequence[zipfile.ZipInfo]) -> set[str]:
    names: set[str] = set()
    portable_names: dict[str, str] = {}
    for info in infos:
        name = _safe_path(info).as_posix()
        if name in names:
            raise _issue("protocol.manifest_invalid", f"ZIP 文件重复：{name}", name)
        portable = unicodedata.normalize("NFC", name).casefold()
        prior = portable_names.get(portable)
        if prior is not None:
            raise _issue(
                "protocol.manifest_invalid",
                f"ZIP 路径存在大小写或 Unicode 冲突：{prior} / {name}",
                name,
            )
        names.add(name)
        portable_names[portable] = name
    return names


def _read_json(archive: zipfile.ZipFile, name: str) -> dict[str, Any]:
    try:
        raw = archive.read(name)
    except KeyError as exc:
        raise _issue("protocol.manifest_invalid", f"缺少文件 {name}", name) from exc
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _issue("protocol.manifest_invalid", f"{name} 不是合法 UTF-8 JSON", name) from exc
    if not isinstance(value, dict):
        raise _issue("protocol.manifest_invalid", f"{name} 根节点必须是对象", name)
    return value


def _topological(descriptors: Mapping[str, Any]) -> list[str]:
    visiting: set[str] = set()
    visited: set[str] = set()
    result: list[str] = []

    def visit(module_id: str) -> None:
        if module_id in visiting:
            raise _issue("module.dependency_missing", f"模块依赖成环：{module_id}")
        if module_id in visited:
            return
        visiting.add(module_id)
        descriptor = descriptors[module_id]
        for dependency in descriptor.depends_on:
            if dependency not in descriptors:
                raise _issue(
                    "module.dependency_missing",
                    f"模块 {module_id} 缺少依赖 {dependency}",
                    f"modules.{module_id}.depends_on",
                )
            if descriptor.enabled and not descriptors[dependency].enabled:
                raise _issue(
                    "module.dependency_disabled",
                    f"模块 {module_id} 的依赖 {dependency} 已关闭",
                    f"modules.{module_id}.depends_on",
                )
            visit(dependency)
        visiting.remove(module_id)
        visited.add(module_id)
        result.append(module_id)

    for current in sorted(descriptors):
        visit(current)
    return result


def _at_path(value: object, path: str) -> object:
    current = value
    for part in str(path or "").split("."):
        if not part:
            continue
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def _short_parts(value: object) -> tuple[str, str] | None:
    text = str(value or "")
    if text.count(":") != 1:
        return None
    entity_type, entity_id = text.split(":", 1)
    if entity_type not in REFERENCE_TYPES or not entity_id:
        return None
    return entity_type, entity_id


def _collect_refs(value: object, *, path: str = "") -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            result.extend(_collect_refs(item, path=child))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, item in enumerate(value):
            result.extend(_collect_refs(item, path=f"{path}[{index}]"))
    elif isinstance(value, str):
        parsed = _short_parts(value)
        if parsed:
            result.append((value, path))
    return result


def _effective_required_modules(
    descriptors: Mapping[str, Any],
    definitions: Mapping[str, Mapping[str, Any]],
    world: Mapping[str, Any],
) -> dict[str, tuple[str, ...]]:
    """Derive modules that cannot be disabled in the current composition.

    ``required`` is not only an author assertion.  A module is also required
    while another enabled module depends on it, while enabled/world content
    references an entity it owns, or while the world lists it in
    ``required_features``.  Recompiling after a consumer is disabled can make
    its former provider optional again.
    """

    reasons: dict[str, set[str]] = {
        module_id: set() for module_id in descriptors
    }
    for module_id, descriptor in descriptors.items():
        if bool(descriptor.required):
            reasons[module_id].add("declared_required")
    for source_id, descriptor in descriptors.items():
        if not bool(descriptor.enabled):
            continue
        for dependency in descriptor.depends_on:
            dependency_id = str(dependency)
            if dependency_id in reasons:
                reasons[dependency_id].add(f"dependency:{source_id}")

    owners: dict[str, str] = {}
    for module_id, descriptor in descriptors.items():
        payload = definitions.get(module_id, {})
        for collection in descriptor.entity_collections:
            entity_type = str(collection.get("type") or "")
            collection_path = str(collection.get("path") or "")
            id_field = str(collection.get("id_field") or "id")
            items = _at_path(payload, collection_path)
            if not isinstance(items, Sequence) or isinstance(
                items,
                (str, bytes),
            ):
                continue
            for raw in items:
                if not isinstance(raw, Mapping):
                    continue
                raw_id = str(raw.get(id_field) or "")
                parsed = _short_parts(raw_id)
                short_ref = (
                    raw_id
                    if parsed
                    else f"{entity_type}:{raw_id}"
                    if entity_type and raw_id
                    else ""
                )
                if short_ref:
                    owners[short_ref] = module_id

    for source_id, descriptor in descriptors.items():
        if not bool(descriptor.enabled):
            continue
        for ref, _path in _collect_refs(definitions.get(source_id, {})):
            owner = owners.get(ref)
            if owner and owner != source_id:
                reasons[owner].add(f"entity_reference:{source_id}")
    for ref, _path in _collect_refs(world):
        owner = owners.get(ref)
        if owner:
            reasons[owner].add("world_reference")

    for raw_requirement in world.get("required_features") or []:
        feature_id = str(raw_requirement).split("@", 1)[0]
        if feature_id in reasons:
            reasons[feature_id].add("required_feature")
    return {
        module_id: tuple(sorted(values))
        for module_id, values in reasons.items()
        if values
    }


def _entity_index(
    namespace: str,
    definitions: Mapping[str, dict[str, Any]],
    descriptors: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[Problem]]:
    entities: dict[str, EntityRef] = {}
    reverse: dict[str, list[dict[str, str]]] = {}
    problems: list[Problem] = []
    for module_id, descriptor in descriptors.items():
        payload = definitions.get(module_id, {})
        for collection in descriptor.entity_collections:
            entity_type = str(collection.get("type") or "")
            collection_path = str(collection.get("path") or "")
            id_field = str(collection.get("id_field") or "id")
            label_field = str(collection.get("label_field") or "label")
            visibility_field = str(collection.get("visibility_field") or "visibility")
            items = _at_path(payload, collection_path)
            if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
                continue
            for index, raw in enumerate(items):
                if not isinstance(raw, Mapping):
                    continue
                raw_id = str(raw.get(id_field) or "")
                parsed = _short_parts(raw_id)
                if parsed:
                    parsed_type, entity_id = parsed
                    if parsed_type != entity_type:
                        problems.append(
                            Problem(
                                problem_id=f"problem-type-{module_id}-{index}",
                                code="reference.type_mismatch",
                                severity="error",
                                message=f"{raw_id} 的类型与声明 {entity_type} 不一致",
                                source=SourceLocation(
                                    descriptor.definitions,
                                    f"{collection_path}[{index}].{id_field}",
                                ),
                                module=module_id,
                                impact="block_publish",
                            )
                        )
                        continue
                else:
                    entity_id = raw_id
                if not entity_id:
                    continue
                entity = EntityRef(
                    namespace=namespace,
                    type=entity_type,
                    id=entity_id,
                    label=str(raw.get(label_field) or raw.get("name") or raw_id),
                    source=SourceLocation(
                        descriptor.definitions,
                        f"{collection_path}[{index}]",
                    ),
                    visibility=str(raw.get(visibility_field) or "public"),
                    command_target=bool(collection.get("command_target")),
                )
                if entity.short_ref in entities:
                    problems.append(
                        Problem(
                            problem_id=f"problem-duplicate-{entity_type}-{entity_id}",
                            code="protocol.manifest_invalid",
                            severity="error",
                            message=f"实体重复：{entity.short_ref}",
                            source=entity.source,
                            entity_ref=entity.canonical_ref,
                            module=module_id,
                            impact="block_publish",
                        )
                    )
                entities[entity.short_ref] = entity

    for module_id, payload in definitions.items():
        for ref, source_path in _collect_refs(payload):
            if ref not in entities:
                problems.append(
                    Problem(
                        problem_id=f"problem-missing-{_sha((module_id + source_path + ref).encode())[:16]}",
                        code="reference.target_missing",
                        severity="error",
                        message=f"目标引用不存在：{ref}",
                        source=SourceLocation(
                            descriptors[module_id].definitions,
                            source_path,
                        ),
                        entity_ref=ref,
                        module=module_id,
                        impact="block_publish",
                        suggested_action="添加目标实体、提供别名或修正引用",
                    )
                )
            else:
                reverse.setdefault(ref, []).append(
                    {
                        "module": module_id,
                        "file": descriptors[module_id].definitions,
                        "path": source_path,
                    }
                )
    return (
        [entity.export() for entity in sorted(entities.values(), key=lambda item: item.canonical_ref)],
        [
            {"target": ref, "sources": sources}
            for ref, sources in sorted(reverse.items())
        ],
        problems,
    )


__all__ = [name for name in globals() if not name.startswith('__')]

