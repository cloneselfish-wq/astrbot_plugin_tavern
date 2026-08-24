"""D1 玩家外显契约：纯 DTO 与投影辅助。

本包只提供“领域事实 → 玩家安全投影”的纯函数与数据结构：

- ``web_views``：PlayerChoiceView / WorldSummaryView / NarrativeControlView /
  ModulePanelView / DeliveryStatusView / ActorFateView / TerminalView /
  TechnicalDetailView / PlayerMessageView；
- 不读取数据库、不发起网络请求、不修改任何状态；
- 普通视图绝不输出稳定 ID、字段 key、JSON、UMO 或 revision；
- 技术字段只通过 ``technical`` 键随 ``include_technical_refs=True`` 输出。

后续接线方（dashboard / web_console / 消息渲染器）应只消费本包的投影结果，
不得自行把原始数据库行拼进玩家可见文本。
"""

from __future__ import annotations

from .common import (
    DEFAULT_COMMAND_PREFIX,
    LEAKAGE_MARKERS,
    clean_label,
    contains_leakage,
    safe_int,
)
from .narrative_document import (
    NARRATIVE_BLOCK_KINDS,
    NARRATIVE_DOCUMENT_SCHEMA_ID,
    NARRATIVE_MODE_BOUNDS,
    LegacyNarrativeText,
    NarrativeBlock,
    NarrativeContinuity,
    NarrativeContractError,
    NarrativeDeliveryPart,
    NarrativeDocument,
    NarrativeRepairError,
    NarrativeSpeaker,
    canonical_narrative_json,
    chunk_narrative_document,
    inspect_narrative_document,
    legacy_text_fallback,
    narrative_document_sha256,
    narrative_document_from_plain_text,
    narrative_document_to_plain_text,
    narrative_fact_sha256,
    narrative_text_sha256,
    parse_narrative_document,
    project_public_narrative_document,
    repair_narrative_document,
)

__all__ = [
    "DEFAULT_COMMAND_PREFIX",
    "LEAKAGE_MARKERS",
    "clean_label",
    "contains_leakage",
    "safe_int",
    "LegacyNarrativeText",
    "NARRATIVE_BLOCK_KINDS",
    "NARRATIVE_DOCUMENT_SCHEMA_ID",
    "NARRATIVE_MODE_BOUNDS",
    "NarrativeBlock",
    "NarrativeContinuity",
    "NarrativeContractError",
    "NarrativeDeliveryPart",
    "NarrativeDocument",
    "NarrativeRepairError",
    "NarrativeSpeaker",
    "canonical_narrative_json",
    "chunk_narrative_document",
    "inspect_narrative_document",
    "legacy_text_fallback",
    "narrative_document_sha256",
    "narrative_document_from_plain_text",
    "narrative_document_to_plain_text",
    "narrative_fact_sha256",
    "narrative_text_sha256",
    "parse_narrative_document",
    "project_public_narrative_document",
    "repair_narrative_document",
]
