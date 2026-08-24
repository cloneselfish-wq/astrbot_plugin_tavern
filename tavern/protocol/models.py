from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .constants import TWP_COMMAND_SCHEMA, TWP_VERSION


@dataclass(frozen=True)
class SourceLocation:
    file: str = ""
    path: str = ""


@dataclass(frozen=True)
class EntityRef:
    namespace: str
    type: str
    id: str
    label: str = ""
    source: SourceLocation = SourceLocation()
    visibility: str = "public"
    command_target: bool = False

    @property
    def short_ref(self) -> str:
        return f"{self.type}:{self.id}"

    @property
    def canonical_ref(self) -> str:
        return f"{self.namespace}:{self.type}:{self.id}"

    def export(self) -> dict[str, Any]:
        value = asdict(self)
        value["short_ref"] = self.short_ref
        value["canonical_ref"] = self.canonical_ref
        return value


@dataclass(frozen=True)
class ModuleDescriptor:
    module_id: str
    api_version: str
    definition_schema: str
    runtime_schema: str
    definitions: str
    commands: str
    events: str
    projections: str
    migration_dir: str
    tests_dir: str
    depends_on: tuple[str, ...] = ()
    read_paths: tuple[str, ...] = ()
    write_paths: tuple[str, ...] = ()
    state_path: str = ""
    state_fields: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    text_collections: tuple[dict[str, Any], ...] = ()
    absence_policy: str = "not_applicable"
    provider_kind: str = "builtin"
    provider_id: str = ""
    required: bool = False
    enabled: bool = True
    entity_collections: tuple[dict[str, Any], ...] = ()

    def export(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Problem:
    problem_id: str
    code: str
    severity: str
    message: str
    source: SourceLocation = field(default_factory=SourceLocation)
    entity_ref: str = ""
    module: str = ""
    impact: str = "diagnostic"
    visibility: str = "author"
    suggested_action: str = ""
    resolved: bool = False

    def export(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CommandEnvelope:
    command_id: str
    domain: str
    action: str
    actor_ref: str = ""
    target_refs: tuple[str, ...] = ()
    payload: dict[str, Any] = field(default_factory=dict)
    operator: str = ""
    reason: str = ""
    visibility: str = "public"
    idempotency_key: str = ""
    expected_revision: int | None = None
    artifact_id: str = ""
    api_version: str = TWP_COMMAND_SCHEMA

    def export(self) -> dict[str, Any]:
        value = asdict(self)
        value["target_refs"] = list(self.target_refs)
        return value


@dataclass(frozen=True)
class CommandPlan:
    plan_hash: str
    base_revision: int
    revision_after: int
    reads: tuple[dict[str, Any], ...] = ()
    conditions: tuple[dict[str, Any], ...] = ()
    changes: tuple[dict[str, Any], ...] = ()
    effects: tuple[dict[str, Any], ...] = ()
    events: tuple[dict[str, Any], ...] = ()
    cascades: tuple[dict[str, Any], ...] = ()
    visibility: dict[str, Any] = field(default_factory=dict)
    requires_confirmation: bool = False
    irreversible: bool = False

    def export(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EventRecord:
    event_id: str
    event_type: str
    operation_id: str
    artifact_id: str
    runtime_revision: int
    sequence: int
    schema: str = TWP_VERSION
    causation_id: str = ""
    correlation_id: str = ""
    parent_event_id: str | None = None
    actor_ref: str = ""
    target_refs: tuple[str, ...] = ()
    visibility: str = "public"
    summary_key: str = ""
    changes_digest: str = ""

    def export(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OperationReceipt:
    operation_id: str
    input_hash: str
    status: str
    artifact_id: str
    revision_before: int
    revision_after: int
    plan_hash: str = ""
    events: tuple[dict[str, Any], ...] = ()
    changes_digest: str = ""
    projection_digest: str = ""
    replayed: bool = False
    created_at: str = ""

    def export(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WorldArtifact:
    artifact_id: str
    source_hash: str
    artifact_hash: str
    dependency_lock_hash: str
    module_load_order: tuple[str, ...]
    entity_index: tuple[dict[str, Any], ...]
    runtime_contract: dict[str, Any]
    command_catalog: dict[str, Any]
    projection_catalog: dict[str, Any]
    conformance: dict[str, Any]
    ui_profile: dict[str, Any] = field(default_factory=dict)
    localization_resources: dict[str, Any] = field(default_factory=dict)
    required_text_keys: tuple[str, ...] = ()
    resolved_text_catalog: dict[str, dict[str, str]] = field(default_factory=dict)
    message_copy_bindings: dict[str, dict[str, str]] = field(default_factory=dict)
    interaction_policy: dict[str, Any] = field(default_factory=dict)
    enabled_modules: tuple[str, ...] = ()
    entity_collections: dict[str, tuple[dict[str, Any], ...]] = field(
        default_factory=dict
    )
    absence_policy: dict[str, str] = field(default_factory=dict)
    localization_reports: dict[str, dict[str, Any]] = field(default_factory=dict)
    compiler_abi: str = "1"
    protocol: str = f"twp@{TWP_VERSION}"

    def export(self) -> dict[str, Any]:
        return asdict(self)
