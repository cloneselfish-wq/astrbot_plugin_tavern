from .common import *
from .archive_reader import *
from .dependencies import *
from .extensions import validate_ui_schema
from ..narrative_styles import normalize_world_narrative_style

import math


_PROFILE_ROLE = re.compile(
    r"^[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)+$"
)
_PROFILE_VISIBILITY = {
    "": "public",
    "public": "public",
    "player": "player",
    "character": "player",
    "party": "party",
    "group": "party",
    "host": "host",
    "dm": "host",
    "author": "admin",
    "admin": "admin",
    "private": "admin",
}
_PROFILE_VISIBILITY_ORDER = {
    "public": 0,
    "player": 1,
    "party": 2,
    "host": 3,
    "admin": 4,
}
_PROFILE_LENS_FEATURES = {
    "scene": ("scene_graph",),
    "quests": ("quest_graph",),
    "clocks": ("time_clock",),
    "relations": ("relationship_graph",),
    "resources": ("resources",),
    "challenge": ("challenge_engine",),
    "progression": ("progression",),
}
_PROFILE_SECTION_FEATURES = {
    "quest_board": ("quest_graph",),
    "clock_board": ("time_clock",),
    "scene_path": ("scene_graph",),
    "relationship_graph": ("relationship_graph",),
    "challenge_board": ("challenge_engine",),
    "progression_board": ("progression",),
}


def _profile_visibility(value: Any) -> str:
    return _PROFILE_VISIBILITY.get(str(value or "").strip().lower(), "admin")


def _profile_effective_visibility(*values: Any) -> str:
    normalized = [_profile_visibility(value) for value in values]
    return max(normalized, key=lambda item: _PROFILE_VISIBILITY_ORDER[item])


def _profile_scale(value: Mapping[str, Any]) -> dict[str, Any] | None:
    minimum = value.get("minimum", value.get("min"))
    maximum = value.get("maximum", value.get("max"))
    if (
        isinstance(minimum, bool)
        or isinstance(maximum, bool)
        or not isinstance(minimum, (int, float))
        or not isinstance(maximum, (int, float))
        or not math.isfinite(float(minimum))
        or not math.isfinite(float(maximum))
        or minimum >= maximum
    ):
        return None
    result: dict[str, Any] = {"min": minimum, "max": maximum}
    unit = str(value.get("unit") or "").strip()
    if unit:
        result["unit"] = unit[:24]
    return result


def _profile_role_registry(
    definitions: Mapping[str, Mapping[str, Any]],
    entity_index: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    roles: dict[str, dict[str, Any]] = {}
    handles: dict[str, str] = {}

    def register(
        role: str,
        handle: str,
        *,
        path: str,
        visibility: Any = "public",
        scale: Mapping[str, Any] | None = None,
        explicit: bool = True,
    ) -> None:
        if _PROFILE_ROLE.fullmatch(role) is None:
            if explicit:
                raise _issue(
                    "protocol.manifest_invalid",
                    f"语义角色格式无效：{role}",
                    path,
                )
            return
        if role in roles:
            if explicit or roles[role]["handle"] != handle:
                raise _issue(
                    "protocol.manifest_invalid",
                    f"语义角色重复声明：{role}",
                    path,
                )
            return
        if handle in handles and handles[handle] != role:
            raise _issue(
                "protocol.manifest_invalid",
                f"compiled handle 被多个角色复用：{handle}",
                path,
            )
        roles[role] = {
            "handle": handle,
            "visibility": _profile_visibility(visibility),
            "scale": dict(scale) if isinstance(scale, Mapping) else None,
        }
        handles[handle] = role

    actor = definitions.get("actor")
    actor = actor if isinstance(actor, Mapping) else {}
    fields = actor.get("fields")
    fields = fields if isinstance(fields, Sequence) and not isinstance(fields, (str, bytes)) else []
    for index, raw in enumerate(fields):
        if not isinstance(raw, Mapping):
            continue
        role = str(raw.get("semantic_role") or "").strip()
        key = str(raw.get("key") or "").strip()
        if role and key:
            register(
                role,
                f"actor-field:{key}",
                path=f"definitions.actor.fields[{index}].semantic_role",
                visibility=raw.get("visibility") or ("admin" if raw.get("private") else "public"),
            )
    stats = actor.get("stats")
    stats = stats if isinstance(stats, Mapping) else {}
    attributes = stats.get("attributes")
    attributes = (
        attributes
        if isinstance(attributes, Sequence) and not isinstance(attributes, (str, bytes))
        else []
    )
    for index, raw in enumerate(attributes):
        if not isinstance(raw, Mapping):
            continue
        key = str(raw.get("key") or raw.get("id") or "").strip()
        role = str(raw.get("semantic_role") or f"attribute.{key}").strip()
        if key:
            register(
                role,
                f"actor-attribute:{key}",
                path=f"definitions.actor.stats.attributes[{index}]",
                visibility=raw.get("visibility"),
                scale=_profile_scale(raw),
            )
    for index, raw in enumerate(entity_index):
        if not isinstance(raw, Mapping):
            continue
        entity_type = str(raw.get("type") or "").strip()
        entity_id = str(raw.get("id") or "").strip()
        role = str(raw.get("semantic_role") or "").strip()
        explicit = bool(role)
        if not role and entity_id:
            prefix = {
                "runtime_effect": "status",
                "fate_state": "status",
                "status": "status",
                "condition": "status",
                "resource": "resource",
                "attribute": "attribute",
                "stat": "attribute",
            }.get(entity_type, "")
            role = f"{prefix}.{entity_id}" if prefix else ""
        if not role:
            continue
        handle = str(
            raw.get("canonical_ref")
            or raw.get("short_ref")
            or f"{entity_type}:{entity_id}"
        )
        register(
            role,
            handle,
            path=f"entity_index[{index}]",
            visibility=raw.get("visibility"),
            scale=_profile_scale(raw),
            explicit=explicit,
        )
    return roles


def compile_ui_profile(
    ui_schema: Mapping[str, Any],
    *,
    definitions: Mapping[str, Mapping[str, Any]],
    entity_index: Sequence[Mapping[str, Any]],
    features: Mapping[str, Any],
    source_hash: str,
) -> dict[str, Any]:
    """Compile author UI declarations into deterministic internal handles."""

    feature_map = {str(key): str(value) for key, value in features.items()}
    schema = validate_ui_schema(ui_schema, module_ids=set(feature_map))
    normalized_source_hash = str(source_hash or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", normalized_source_hash) is None:
        raise _issue(
            "protocol.manifest_invalid",
            "ui_profile source_hash 必须是 SHA-256",
            "ui_schema",
        )
    registry = _profile_role_registry(definitions, entity_index)
    referenced_roles: set[str] = set()
    referenced_visibility: dict[str, str] = {}
    required_features: set[str] = set()
    declared_widgets: set[str] = set()
    visibilities: list[str] = ["public"]

    def role_entry(role: str, path: str, declared_visibility: Any) -> dict[str, Any]:
        entry = registry.get(role)
        if entry is None:
            raise _issue(
                "protocol.manifest_invalid",
                f"ui_schema 引用了未登记 semantic role：{role}",
                path,
            )
        referenced_roles.add(role)
        visibility = _profile_effective_visibility(
            entry.get("visibility"), declared_visibility
        )
        referenced_visibility[role] = _profile_effective_visibility(
            referenced_visibility.get(role), visibility
        )
        visibilities.append(visibility)
        return {
            "role_handle": str(entry["handle"]),
            "visibility": visibility,
            "scale": entry.get("scale"),
        }

    pages: list[dict[str, Any]] = []
    adaptive = bool(
        schema["party"]
        or schema["actor_detail"]
        or schema["live_lenses"]
        or schema["status_taxonomy"]
        or schema["visualizations"]
        or schema["presentation"]
        or schema["density"] != "standard"
    )
    for page in schema["pages"]:
        sections: list[dict[str, Any]] = []
        for section in page["sections"]:
            compiled_section = dict(section)
            kind = str(section["kind"])
            declared_widgets.add(kind)
            if kind not in {
                "hero", "text", "metric_grid", "module_panel",
                "entity_list", "timeline", "notice",
            }:
                adaptive = True
            required = list(section.get("requires") or ())
            if not required:
                required = list(_PROFILE_SECTION_FEATURES.get(kind, ()))
            module_id = str(section.get("module_id") or "")
            if module_id:
                required.append(module_id)
            compiled_section["requires"] = sorted(set(required))
            required_features.update(compiled_section["requires"])
            visibility = _profile_visibility(section.get("visibility"))
            compiled_section["visibility"] = visibility
            visibilities.append(visibility)
            sections.append(compiled_section)
        compiled_page = {"id": page["id"], "sections": sections}
        if page.get("label"):
            compiled_page["label"] = page["label"]
        pages.append(compiled_page)

    party = {
        key: value
        for key, value in schema["party"].items()
        if key != "identity_facets"
    }
    facets: list[dict[str, Any]] = []
    for index, facet in enumerate(schema["party"].get("identity_facets") or ()):
        resolved = role_entry(
            str(facet["role"]),
            f"ui_schema.party.identity_facets[{index}].role",
            facet.get("visibility"),
        )
        facets.append(
            {
                "role_handle": resolved["role_handle"],
                "label": facet["label"],
                "priority": facet["priority"],
                "visibility": resolved["visibility"],
            }
        )
    if facets:
        declared_widgets.add("party")
        party["identity_facets"] = sorted(
            facets, key=lambda item: (item["priority"], item["role_handle"])
        )
    actor_detail = dict(schema["actor_detail"])
    if actor_detail:
        declared_widgets.add("actor_detail")

    statuses: list[dict[str, Any]] = []
    for index, status in enumerate(schema["status_taxonomy"]):
        resolved = role_entry(
            str(status["role"]),
            f"ui_schema.status_taxonomy[{index}].role",
            status.get("visibility"),
        )
        statuses.append(
            {
                "role_handle": resolved["role_handle"],
                "label": status["label"],
                "tone": status["tone"],
                "symbol": status["symbol"],
                "visibility": resolved["visibility"],
            }
        )
    statuses.sort(key=lambda item: item["role_handle"])

    visualizations: list[dict[str, Any]] = []
    for visual_index, visual in enumerate(schema["visualizations"]):
        role_handles: list[str] = []
        role_visibilities: list[str] = []
        source_scales: dict[str, dict[str, Any]] = {}
        for role_index, role in enumerate(visual["roles"]):
            resolved = role_entry(
                str(role),
                f"ui_schema.visualizations[{visual_index}].roles[{role_index}]",
                visual.get("visibility"),
            )
            role_handles.append(resolved["role_handle"])
            role_visibilities.append(resolved["visibility"])
            if isinstance(resolved.get("scale"), Mapping):
                scale = dict(resolved["scale"])
                source_scales[json.dumps(scale, sort_keys=True)] = scale
        if len(source_scales) > 1:
            raise _issue(
                "protocol.manifest_invalid",
                "visualization roles 使用了不同量纲或尺度",
                f"ui_schema.visualizations[{visual_index}].roles",
            )
        declared_scale = visual.get("scale")
        source_scale = next(iter(source_scales.values()), None)
        if declared_scale and source_scale and dict(declared_scale) != source_scale:
            raise _issue(
                "protocol.manifest_invalid",
                "visualization scale 与角色定义不一致",
                f"ui_schema.visualizations[{visual_index}].scale",
            )
        compiled_visual = {
            "id": visual["id"],
            "kind": visual["kind"],
            "title": visual["title"],
            "role_handles": role_handles,
            "scale": dict(declared_scale or source_scale or {}),
            "fallback": visual["fallback"],
            "visibility": max(role_visibilities, key=lambda item: _PROFILE_VISIBILITY_ORDER[item]),
        }
        visualizations.append(compiled_visual)
        declared_widgets.add("attribute_chart")
    visualizations.sort(key=lambda item: item["id"])

    lenses = [dict(item) for item in schema["live_lenses"]]
    lens_ids = {str(item["id"]) for item in lenses}
    fallback_lenses: list[str] = []
    for lens_id, label, order in (("party", "小队", 0), ("replay", "回放", 900)):
        if lens_id not in lens_ids:
            lenses.append(
                {"id": lens_id, "label": label, "requires": [], "required": True, "order": order}
            )
            fallback_lenses.append(lens_id)
    for lens in lenses:
        lens_id = str(lens["id"])
        required = list(lens.get("requires") or ())
        if not required:
            required = list(_PROFILE_LENS_FEATURES.get(lens_id, ()))
        lens["requires"] = sorted(set(required))
        required_features.update(lens["requires"])
        declared_widgets.add(f"lens.{lens_id}")
    lenses.sort(key=lambda item: (int(item.get("order") or 0), str(item["id"])))
    if adaptive:
        required_features.add("adaptive_ui")
    missing = sorted(feature for feature in required_features if feature not in feature_map)
    if missing:
        raise _issue(
            "protocol.manifest_invalid",
            "ui_schema 缺少已启用 feature：" + "、".join(missing),
            "ui_schema",
        )

    public_roles = {
        role: {
            "handle": registry[role]["handle"],
            "visibility": referenced_visibility[role],
        }
        for role in sorted(referenced_roles)
    }
    compiled_surfaces: list[dict[str, Any]] = []
    for surface in schema["surfaces"]:
        compiled_surfaces.append(
            {
                "surface_key": "surface_" + hashlib.sha256(
                    f"{normalized_source_hash}:{surface['id']}".encode()
                ).hexdigest()[:24],
                "surface_id": surface["id"],
                "capability_key": surface["capability_ref"],
                "module_id": surface["module_id"],
                "placements": list(surface["placements"]),
                "component_kind": surface["component_kind"],
                "data_kind": surface["data_kind"],
                "group": surface["group"],
                "label": surface["copy"]["title"],
                "summary": surface["copy"]["summary"],
                "usage": surface["usage"],
                "audience_scopes": list(surface["audience"]),
                "definition_projection": surface["definition_binding"]["projection"],
                "runtime_projection": surface["runtime_binding"]["projection"],
                "readme_sections": list(surface["readme_sections"]),
                "empty_policy": surface["empty_policy"],
                "refresh": dict(surface["refresh"]),
                "mobile_presentation": surface["mobile_presentation"],
                "visual_recipe": surface["visual_recipe"],
                "copy": dict(surface["copy"]),
                "required": bool(surface["required"]),
                "order": int(surface["order"]),
            }
        )
    surface_seed = {
        "schema": f"tavern-ui-surface-manifest/{TWP_VERSION}",
        "world_revision": normalized_source_hash,
        "profile_revision": normalized_source_hash,
        "component_registry_version": (
            compiled_surfaces[0].get("component_registry_version")
            if compiled_surfaces
            else TWP_VERSION
        ) or TWP_VERSION,
        "surfaces": compiled_surfaces,
    }
    surface_seed["manifest_revision"] = "sha256:" + hashlib.sha256(
        _canonical(surface_seed)
    ).hexdigest()
    surface_seed["indexes"] = {
        "capability": {
            capability: [item["surface_key"] for item in compiled_surfaces if item["capability_key"] == capability]
            for capability in sorted({item["capability_key"] for item in compiled_surfaces})
        },
        "placement": {
            placement: [item["surface_key"] for item in compiled_surfaces if placement in item["placements"]]
            for placement in sorted({placement for item in compiled_surfaces for placement in item["placements"]})
        },
        "event": {
            event: [item["surface_key"] for item in compiled_surfaces if event in item["refresh"].get("event_types", [])]
            for event in sorted({event for item in compiled_surfaces for event in item["refresh"].get("event_types", [])})
        },
    }
    profile: dict[str, Any] = {
        "schema": f"twp-ui-profile/{TWP_VERSION}",
        "density": schema["density"],
        "empty_policy": schema["empty_policy"],
        "pages": pages,
        "party": party,
        "actor_detail": actor_detail,
        "live_lenses": lenses,
        "status_taxonomy": statuses,
        "visualizations": visualizations,
        "presentation": dict(schema["presentation"]),
        "declared_widgets": sorted(declared_widgets),
        "required_features": sorted(required_features),
        "public_field_roles": public_roles,
        "localization_keys": [],
        "visibility_ceiling": max(
            visibilities, key=lambda item: _PROFILE_VISIBILITY_ORDER[item]
        ),
        "fallback_profile": {
            "used": bool(fallback_lenses),
            "injected_lenses": fallback_lenses,
            "empty_policy": "omit-unsupported",
            "attribute_fallback": "list",
        },
        "source_hash": normalized_source_hash,
        "ui_surface_manifest": surface_seed,
    }
    profile["profile_hash"] = hashlib.sha256(_canonical(profile)).hexdigest()
    return profile


def compile_readme_index(
    archive: zipfile.ZipFile,
    names: set[str],
    *,
    world_slug: str,
) -> dict[str, Any]:
    """Validate the Schema 12 README author source and its generated index."""

    manifest_path = "content/readme/manifest.json"
    index_path = "compiled/readme_index.json"
    if manifest_path not in names or index_path not in names or "README.md" not in names:
        raise _issue(
            "world.readme.audience_invalid",
            "Schema 12 世界包必须包含结构化 README manifest、compiled index 和根 README",
            manifest_path,
        )
    manifest = _read_json(archive, manifest_path)
    if int(manifest.get("schema_version") or 0) != 1:
        raise _issue("world.readme.audience_invalid", "README manifest schema_version 必须为 1", manifest_path)
    if str(manifest.get("world_key") or "") != world_slug:
        raise _issue("world.readme.audience_invalid", "README world_key 与世界 slug 不一致", manifest_path)
    sections = manifest.get("sections")
    if not isinstance(sections, list) or not 1 <= len(sections) <= 512:
        raise _issue("world.readme.audience_invalid", "README sections 必须为 1 至 512 项", manifest_path)
    index = _read_json(archive, index_path)
    compiled = index.get("sections")
    if not isinstance(compiled, list) or len(compiled) != len(sections):
        raise _issue("world.readme.audience_invalid", "README source/index 章节数量不一致", index_path)
    source_by_key: dict[str, Mapping[str, Any]] = {}
    allowed_audience = {"player", "host", "author", "admin"}
    for position, raw in enumerate(sections):
        if not isinstance(raw, Mapping):
            raise _issue("world.readme.audience_invalid", "README section 必须是对象", manifest_path)
        key = str(raw.get("key") or "")
        audience = str(raw.get("audience") or "")
        body_ref = str(raw.get("body_ref") or "")
        if not re.fullmatch(r"[a-z][a-z0-9]*(?:\.[a-z][a-z0-9]*)+", key) or key in source_by_key:
            raise _issue("world.readme.audience_invalid", "README section key 缺失、重复或格式无效", manifest_path)
        if audience not in allowed_audience:
            raise _issue("world.readme.audience_invalid", "README audience 缺失或未知", manifest_path)
        full_ref = "content/readme/" + body_ref
        if body_ref.startswith(("/", "\\")) or ".." in PurePosixPath(body_ref).parts or full_ref not in names:
            raise _issue("world.readme.audience_invalid", "README body_ref 越界或文件缺失", manifest_path)
        body = archive.read(full_ref)
        if len(body) > 128 * 1024:
            raise _issue("world.readme.audience_invalid", "README 单章超过 128 KiB", full_ref)
        body.decode("utf-8")
        source_by_key[key] = raw
        compiled_item = compiled[position]
        if not isinstance(compiled_item, Mapping) or str(compiled_item.get("key") or "") != key:
            raise _issue("world.readme.audience_invalid", "README source/index 顺序或 key 不一致", index_path)
        digest = "sha256:" + _sha(body)
        if str(compiled_item.get("audience") or "") != audience or str(compiled_item.get("body_digest") or "") != digest:
            raise _issue("world.readme.audience_invalid", "README index audience 或 digest 不一致", index_path)
    return {
        "schema_version": 1,
        "world_key": world_slug,
        "readme_revision": str(index.get("readme_revision") or ""),
        "sections": [dict(item) for item in compiled],
    }

def inspect_twp_archive(
    path: Path,
    *,
    overrides: Mapping[str, bool] | None = None,
) -> dict[str, Any]:
    source = Path(path)
    if not source.is_file() or source.stat().st_size > MAX_ARCHIVE_BYTES:
        raise _issue("protocol.manifest_invalid", "TWP ZIP 不存在或超过大小限制")
    overrides = {str(key): bool(value) for key, value in (overrides or {}).items()}
    with zipfile.ZipFile(source) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_FILES:
            raise _issue("protocol.manifest_invalid", "TWP ZIP 文件数量超过限制")
        names = _safe_archive_names(infos)
        total = 0
        for info in infos:
            member = _safe_path(info)
            name = member.as_posix()
            if info.is_dir():
                continue
            if info.file_size > MAX_MEMBER_BYTES:
                raise _issue("protocol.manifest_invalid", f"文件超过大小限制：{name}", name)
            total += info.file_size
            if total > MAX_UNCOMPRESSED_BYTES:
                raise _issue("protocol.manifest_invalid", "TWP ZIP 解压后超过大小限制")
            if PurePosixPath(name).suffix.lower() not in SAFE_SUFFIXES:
                raise _issue("protocol.manifest_invalid", f"不允许的文件类型：{name}", name)

        manifest = validate_manifest(_read_json(archive, "tavern-world.json"))
        integrity = manifest.get("integrity")
        integrity = dict(integrity) if isinstance(integrity, Mapping) else {}
        if str(integrity.get("algorithm") or "").lower() != "sha256":
            raise _issue(
                "protocol.integrity_mismatch",
                "integrity.algorithm 必须为 sha256",
                "tavern-world.json.integrity.algorithm",
            )
        expected_files = integrity.get("files")
        expected_files = dict(expected_files) if isinstance(expected_files, Mapping) else {}
        actual_files = {
            info.filename.rstrip("/")
            for info in infos
            if not info.is_dir()
            and info.filename.rstrip("/") != "tavern-world.json"
        }
        if not expected_files:
            raise _issue(
                "protocol.integrity_mismatch",
                "integrity.files 不能为空",
                "tavern-world.json.integrity.files",
            )
        if set(expected_files) != actual_files:
            missing = sorted(actual_files - set(expected_files))
            extra = sorted(set(expected_files) - actual_files)
            detail = []
            if missing:
                detail.append("清单缺少：" + "、".join(missing[:8]))
            if extra:
                detail.append("清单多出：" + "、".join(extra[:8]))
            raise _issue(
                "protocol.integrity_mismatch",
                "完整性清单必须精确覆盖全部非 manifest 文件；"
                + "；".join(detail),
                "tavern-world.json.integrity.files",
            )
        for name, expected in expected_files.items():
            if name not in names:
                raise _issue("asset.missing", f"完整性清单文件不存在：{name}", str(name))
            actual = _sha(archive.read(str(name)))
            if actual != str(expected):
                raise _issue(
                    "protocol.integrity_mismatch",
                    f"文件哈希不匹配：{name}",
                    str(name),
                )

        world = _read_json(archive, str(manifest["world"]["entry"]))
        descriptors = {}
        definitions: dict[str, dict[str, Any]] = {}
        module_exports = []
        for declaration in manifest["modules"]:
            module_id = str(declaration.get("module_id") or "")
            entry = str(declaration.get("entry") or "")
            descriptor_raw = _read_json(archive, entry)
            descriptor_raw.setdefault("module_id", module_id)
            descriptor_raw["enabled"] = overrides.get(
                module_id,
                bool(declaration.get("enabled", True)),
            )
            descriptor_raw["required"] = bool(declaration.get("required"))
            descriptor = parse_module_descriptor(descriptor_raw, path=entry)
            if descriptor.module_id in descriptors:
                raise _issue("protocol.manifest_invalid", f"模块重复：{descriptor.module_id}", entry)
            definitions_path = str(PurePosixPath(entry).parent / descriptor.definitions)
            definitions[descriptor.module_id] = _read_json(archive, definitions_path)
            descriptors[descriptor.module_id] = descriptor
            module_exports.append(
                {
                    "id": descriptor.module_id,
                    "module_id": descriptor.module_id,
                    "version": descriptor.api_version,
                    "api_version": descriptor.api_version,
                    "enabled": descriptor.enabled,
                    "required": descriptor.required,
                    "depends_on": list(descriptor.depends_on),
                    "entry": entry,
                    "absence_policy": descriptor.absence_policy,
                    "runtime_schema": descriptor.runtime_schema,
                    "state_path": descriptor.state_path,
                    "state_fields": list(descriptor.state_fields),
                    "write_paths": list(descriptor.write_paths),
                    "capabilities": list(descriptor.capabilities),
                    "text_collections": [
                        dict(item) for item in descriptor.text_collections
                    ],
                    "provider": {
                        "kind": descriptor.provider_kind,
                        "id": descriptor.provider_id,
                    },
                }
            )
        required_reasons = _effective_required_modules(
            descriptors,
            definitions,
            world,
        )
        for module_id, descriptor in tuple(descriptors.items()):
            effective_required = module_id in required_reasons
            descriptor = replace(
                descriptor,
                required=effective_required,
            )
            descriptors[module_id] = descriptor
            if effective_required and not descriptor.enabled:
                raise _issue(
                    "module.dependency_disabled",
                    f"当前世界组合要求模块保持启用：{module_id}",
                    f"modules.{module_id}",
                    "请先关闭依赖该模块的功能，或移除对其实体的引用。",
                )
        for item in module_exports:
            module_id = str(item["module_id"])
            item["required"] = module_id in required_reasons
        load_order = _topological(descriptors)
        enabled_load_order = [
            module_id
            for module_id in load_order
            if descriptors[module_id].enabled
        ]
        extension_contract = validate_extensions(
            manifest,
            module_ids=set(descriptors),
        )
        compiled_summary_metrics = compile_summary_metrics(
            extension_contract["summary_metrics"],
            definitions,
        )

        namespace = str(manifest["identity"]["namespace"])
        entity_index, reverse_references, problems = _entity_index(
            namespace,
            {
                module_id: definitions[module_id]
                for module_id in enabled_load_order
            },
            {
                module_id: descriptors[module_id]
                for module_id in enabled_load_order
            },
        )
        blocking = [problem for problem in problems if problem.impact == "block_publish"]
        if blocking:
            first = blocking[0]
            raise _issue(first.code, first.message, first.source.path)

        ai_companion_contract = validate_ai_companions(
            world.get("ai_companions"),
            actor_definitions=definitions.get("actor", {}),
        )
        world_slug = _validate_world_slug(world)
        rules = deepcopy(
            world.get("rules") if isinstance(world.get("rules"), Mapping) else {}
        )
        for module_id, payload in definitions.items():
            if descriptors[module_id].enabled:
                rules[module_id] = deepcopy(payload.get("config") or payload)
        source_protocol = rules.get("protocol")
        source_protocol = (
            dict(source_protocol) if isinstance(source_protocol, Mapping) else {}
        )
        source_features = source_protocol.get("features")
        feature_map = (
            dict(source_features) if isinstance(source_features, Mapping) else {}
        )
        # Package module feature flags are derived from the effective module
        # graph.  A world/core.json snapshot may describe the default build,
        # but must not keep a disabled module advertised after an override.
        for module_id in descriptors:
            feature_map.pop(module_id, None)
        feature_map.update(
            {
                module_id: descriptors[module_id].api_version
                for module_id in enabled_load_order
            }
        )
        for requirement in world.get("required_features") or []:
            feature_id = str(requirement).split("@>=", 1)[0]
            if feature_id in FEATURE_VERSIONS:
                feature_map.setdefault(feature_id, FEATURE_VERSIONS[feature_id])
        for feature_id, supported_version in FEATURE_VERSIONS.items():
            source_key = "action_types" if feature_id == "action_intents" else feature_id
            if source_key in rules:
                feature_map.setdefault(feature_id, supported_version)
        rules.pop("character_card", None)
        rules["internal_world_model_revision"] = WORLD_SCHEMA_VERSION
        rules["protocol"] = {
            "name": "twp",
            "core": TWP_CORE_VERSION,
            "version": TWP_VERSION,
            "compiler_abi": TWP_COMPILER_ABI,
            "maturity": TWP_MATURITY,
            "features": feature_map,
        }
        actor = rules.get("actor")
        if isinstance(actor, Mapping):
            actor = deepcopy(dict(actor))
            try:
                preset_catalog = normalize_preset_libraries(
                    actor,
                    strict=True,
                )
            except PresetLibraryContractError as exc:
                raise _issue(exc.code, str(exc), exc.path) from exc
            actor["preset_libraries"] = preset_catalog["items"]
            actor["preset_library_metadata_complete"] = preset_catalog[
                "metadata_complete"
            ]
            actor["preset_library_problems"] = preset_catalog["problems"]
            rules["actor"] = actor
        opening_scene_text = _validate_scene_contracts(rules, world)
        identity = manifest["identity"]
        localization_world = {
            **world,
            "name": str(identity.get("name") or world.get("name") or ""),
            "rules": rules,
        }
        try:
            localization = compile_localization(
                localization_world,
                bundle_loader=lambda bundle_path: _read_json(archive, bundle_path),
                module_text_collections={
                    module_id: tuple(descriptors[module_id].text_collections)
                    for module_id in enabled_load_order
                },
            )
        except (KeyError, ValueError) as exc:
            raise _issue(
                "localization.invalid",
                str(exc),
                "rules.localization",
                "请修复默认语言包并确保必需玩家文本覆盖率为 100%",
            ) from exc
        localization_config = rules.get("localization")
        localization_config = (
            dict(localization_config)
            if isinstance(localization_config, Mapping)
            else {}
        )
        localization_config.update(
            {
                "resources": localization["resources"],
                "resolved_text_catalog": localization["resolved_text_catalog"],
                "reports": localization["reports"],
                "catalog_hash": localization["catalog_hash"],
            }
        )
        rules["localization"] = localization_config
        default_locale = str(
            localization["resources"].get("default_locale") or "zh-CN"
        )
        default_text_catalog = localization["resolved_text_catalog"].get(
            default_locale,
            {},
        )
        try:
            message_copy_bindings, interaction_policy = (
                _compile_message_copy_contract(
                    definitions.get("chat_experience"),
                    default_catalog=default_text_catalog,
                )
            )
        except ValueError as exc:
            raise _issue(
                "protocol.message_copy_invalid",
                str(exc),
                "modules/chat_experience/definitions.json",
                "请使用已注册的消息类型与插槽，并让 text_id 进入默认语言冻结目录",
            ) from exc
        rules["message_copy_bindings"] = message_copy_bindings
        rules["interaction_policy"] = interaction_policy
        source_hashes = {
            name: _sha(archive.read(name))
            for name in sorted(names)
            if name != "tavern-world.json" and not name.endswith("/")
        }
        source_hash = _sha(_canonical(source_hashes))
        ui_profile = compile_ui_profile(
            extension_contract["ui_schema"],
            definitions=definitions,
            entity_index=entity_index,
            features=feature_map,
            source_hash=source_hash,
        )
        readme_index = compile_readme_index(
            archive,
            names,
            world_slug=world_slug,
        )
        ui_surface_manifest = dict(
            ui_profile.get("ui_surface_manifest") or {}
        )
        dependency_lock = manifest.get("dependencies") or []
        dependency_lock_hash = _sha(_canonical(dependency_lock))
        runtime_contract = {
            module_id: {
                "schema": descriptors[module_id].runtime_schema,
                "state_path": descriptors[module_id].state_path,
                "state_fields": list(descriptors[module_id].state_fields),
                "write_paths": list(descriptors[module_id].write_paths),
                "absence_policy": descriptors[module_id].absence_policy,
                "capabilities": list(descriptors[module_id].capabilities),
                "provider": {
                    "kind": descriptors[module_id].provider_kind,
                    "id": descriptors[module_id].provider_id,
                },
            }
            for module_id in enabled_load_order
        }
        capability_index = {
            capability: module_id
            for module_id in enabled_load_order
            for capability in descriptors[module_id].capabilities
        }
        entity_collections = {
            module_id: tuple(
                dict(item)
                for item in descriptors[module_id].entity_collections
            )
            for module_id in enabled_load_order
        }
        absence_policy = {
            module_id: descriptors[module_id].absence_policy
            for module_id in enabled_load_order
        }
        artifact_seed = {
            "protocol": f"twp@{TWP_VERSION}",
            "compiler_abi": TWP_COMPILER_ABI,
            "source_hash": source_hash,
            "dependency_lock_hash": dependency_lock_hash,
            "module_load_order": enabled_load_order,
            "entity_index": entity_index,
            "runtime_contract": runtime_contract,
            "capability_index": capability_index,
            "localization_catalog_hash": localization["catalog_hash"],
            "localization_resources": localization["resources"],
            "required_text_keys": localization["required_text_keys"],
            "message_copy_bindings": message_copy_bindings,
            "interaction_policy": interaction_policy,
            "enabled_modules": enabled_load_order,
            "module_contracts": module_exports,
            "entity_collections": entity_collections,
            "absence_policy": absence_policy,
            "extension_contract": extension_contract,
            "ui_profile": ui_profile,
            "ui_surface_manifest": ui_surface_manifest,
            "readme_index": readme_index,
            "summary_metrics": compiled_summary_metrics,
            "ai_companions": ai_companion_contract,
        }
        artifact_hash = _sha(_canonical(artifact_seed))
        artifact_id = (
            f"{identity['package_id']}@{identity['content_version']}+{artifact_hash[:16]}"
        )
        artifact = WorldArtifact(
            artifact_id=artifact_id,
            source_hash=source_hash,
            artifact_hash=artifact_hash,
            dependency_lock_hash=dependency_lock_hash,
            module_load_order=tuple(enabled_load_order),
            entity_index=tuple(entity_index),
            runtime_contract=runtime_contract,
            command_catalog={"items": command_catalog()},
            projection_catalog={
                "viewer_roles": ["player", "character", "dm", "admin", "author", "remote"],
                "purposes": ["chat", "web", "remote", "export", "diagnostic"],
            },
            conformance={
                "declared_tests": list(manifest.get("tests") or []),
                "status": "compiled",
            },
            localization_resources=localization["resources"],
            required_text_keys=tuple(localization["required_text_keys"]),
            resolved_text_catalog=localization["resolved_text_catalog"],
            message_copy_bindings=message_copy_bindings,
            interaction_policy=interaction_policy,
            enabled_modules=tuple(enabled_load_order),
            entity_collections=entity_collections,
            absence_policy=absence_policy,
            localization_reports=localization["reports"],
            ui_profile=ui_profile,
        )
        rules_digest_value: dict[str, Any] | None = None
        if RULES_DIGEST_PATH in names:
            try:
                raw_digest = _read_json(archive, RULES_DIGEST_PATH)
            except TwpPackageError as exc:
                raise _issue(
                    "world.rules_digest_invalid",
                    f"{RULES_DIGEST_PATH} 不是合法 UTF-8 JSON："
                    f"{exc.issue.message}",
                    RULES_DIGEST_PATH,
                ) from exc
            digest_issues = structural_issues(raw_digest)
            if digest_issues:
                raise _issue(
                    "world.rules_digest_invalid",
                    f"{RULES_DIGEST_PATH} 结构不符合 {RULES_DIGEST_SCHEMA}："
                    + "；".join(digest_issues),
                    RULES_DIGEST_PATH,
                    "请重新构建世界包（D1-DATA-010）或移除损坏的摘要文件",
                )
            rules_digest_value = raw_digest
        compiled_world = {
            **world,
            "slug": world_slug,
            "opening_scene": opening_scene_text,
            "package_id": identity["package_id"],
            "namespace": identity["namespace"],
            "name": identity["name"],
            "world_content_version": identity["content_version"],
            "content_version": identity["content_version"],
            "minimum_plugin_version": manifest["minimum_plugin_version"],
            "protocol": {
                **dict(manifest["protocol"]),
                "version": TWP_VERSION,
                "features": rules["protocol"]["features"],
            },
            "package_format": TWP_PACKAGE_FORMAT,
            "artifact_id": artifact_id,
            "artifact_hash": artifact_hash,
            "source_hash": source_hash,
            "artifact_schema": TWP_ARTIFACT_SCHEMA,
            "compiled_world_schema": TWP_COMPILED_WORLD_SCHEMA,
            "runtime_schema": TWP_RUNTIME_SCHEMA,
            "internal_world_model_revision": WORLD_SCHEMA_VERSION,
            "rules": rules,
            "twp_modules": module_exports,
            "runtime_contract": runtime_contract,
            "capability_index": capability_index,
            "resolved_text_catalog": localization["resolved_text_catalog"],
            "message_copy_bindings": message_copy_bindings,
            "interaction_policy": interaction_policy,
            "enabled_modules": enabled_load_order,
            "entity_collections": entity_collections,
            "absence_policy": absence_policy,
            "required_text_keys": localization["required_text_keys"],
            "localization_metadata": localization["resources"],
            "localization_reports": localization["reports"],
            "localization_catalog_hash": localization["catalog_hash"],
            "entity_index": entity_index,
            "reverse_references": reverse_references,
            "summary_metrics": compiled_summary_metrics,
            "ui_profile": ui_profile,
            "ui_surface_manifest": ui_surface_manifest,
            "readme_index": readme_index,
            "projection_contracts": extension_contract[
                "projection_contracts"
            ],
            "entity_candidate_policy": extension_contract[
                "entity_candidate_policy"
            ],
            "module_migrations": extension_contract["migrations"],
            "ai_companions": ai_companion_contract,
        }
        narrative_style_path = "content/narrative_style.json"
        if narrative_style_path in names:
            try:
                compiled_world["narrative_style"] = (
                    normalize_world_narrative_style(
                        _read_json(archive, narrative_style_path)
                    )
                )
            except (TwpPackageError, ValueError) as exc:
                message = (
                    exc.issue.message
                    if isinstance(exc, TwpPackageError)
                    else str(exc)
                )
                raise _issue(
                    "world.narrative_style_invalid",
                    f"世界叙事文风无效：{message}",
                    narrative_style_path,
                    "请修正世界包中的五档默认文风后重新构建",
                ) from exc
        if rules_digest_value is not None:
            compiled_world["rules_digest"] = rules_digest_value
        compiled_actor = rules.get("actor")
        if isinstance(compiled_actor, Mapping):
            declaration_issues = validate_field_count_declarations(
                compiled_actor
            )
            if declaration_issues:
                issue = declaration_issues[0]
                raise _issue(
                    str(issue["code"]),
                    (
                        f"{issue['message']}；期望 {issue.get('expected')}，"
                        f"实际 {issue.get('actual')}"
                    ),
                    str(issue["path"]),
                    "请修正作者源 character_presets.json 后重新构建世界包",
                )
            try:
                compiled_actor["field_account"] = field_account(
                    compiled_actor
                )
            except FieldAccountingError as exc:
                raise _issue(
                    exc.code,
                    str(exc),
                    exc.path,
                    "请修正作者源角色字段定义后重新构建世界包",
                ) from exc
        opening_issues = opening_contract_issues(compiled_world)
        if opening_issues:
            issue = opening_issues[0]
            raise _issue(
                issue.code,
                issue.message,
                issue.path,
                issue.recovery,
            )
        compiled_initial = initialize_runtime(
            compiled_world,
            world.get("initial_state") or {},
        )
        if not isinstance(compiled_initial.get("runtime"), Mapping):
            raise _issue(
                "runtime.schema_invalid",
                "TWP 运行态初始化未生成 runtime 对象",
                str(manifest["world"]["entry"]),
            )
        compiled_world["initial_state"] = compiled_initial
        validate_world_contract(compiled_world)
        artifact_value = artifact.export()
        artifact_value["compiled_world_hash"] = _sha(_canonical(compiled_world))
        return {
            "compatible": True,
            "manifest": manifest,
            "modules": module_exports,
            "load_order": load_order,
            "compiled_world": compiled_world,
            "artifact": artifact_value,
            "artifact_hash": artifact_hash,
            "source_hash": source_hash,
            "content_hash": artifact_hash,
            "entity_index": entity_index,
            "reverse_references": reverse_references,
            "problems": [problem.export() for problem in problems],
            "issues": [],
            "conformance": artifact.conformance,
            "external_dependencies": list(manifest.get("dependencies") or []),
            "summary": {
                "package_id": identity["package_id"],
                "name": identity["name"],
                "version": identity["content_version"],
                "protocol": TWP_VERSION,
                "enabled_modules": sum(bool(item["enabled"]) for item in module_exports),
                "declared_modules": len(module_exports),
                "entities": len(entity_index),
                "assets": len(manifest.get("assets") or []),
                "files": len([info for info in infos if not info.is_dir()]),
                "uncompressed_bytes": total,
            },
        }


__all__ = [name for name in globals() if not name.startswith('__')]

