"""D1-DATA-010 世界核心权威摘要（rules_digest）的运行时加载与投影。

构建器把作者源压缩为 ``world/rules_digest.json``（schema
``twp-rules-digest/1.0.0-rc10``），编译世界时由协议编译器附加为
``compiled_world["rules_digest"]``。本模块负责：

- 从编译世界加载并校验摘要；
- 把职业、社会身份、地区、阵营、能力边界、技能成长轨迹整理为紧凑的
  内部规则摘要，供模型系统上下文使用（见 ``prompts.system_prompt``）；
- 摘要缺失或损坏时安全降级：不注入伪造内容，只把技术诊断信息提供给
  开发者/管理员（``rules_digest_diagnostics``）。

本模块的输出只进入模型上下文或技术诊断；普通玩家消息与 WebUI 投影
不经过这里的任何输出，因此不会泄露内部稳定 ID 或原始 JSON。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

RULES_DIGEST_SCHEMA = "twp-rules-digest/1.0.0-rc10"
RULES_DIGEST_PATH = "world/rules_digest.json"

_SECTION_KEYS = (
    "professions",
    "social_identities",
    "regions",
    "factions",
    "capability_boundaries",
    "ability_tracks",
)

_SECTION_LABELS = {
    "professions": "职业",
    "social_identities": "社会身份",
    "regions": "地区",
    "factions": "阵营",
    "capability_boundaries": "能力边界",
    "ability_tracks": "技能成长轨迹",
}

_ENTITY_SECTIONS = frozenset(
    {"professions", "social_identities", "regions", "factions"}
)


@dataclass(frozen=True)
class RulesDigestState:
    """摘要加载结果。

    ``status``：

    - ``ready``：结构有效，已整理可注入的摘要内容；
    - ``absent``：世界包未附带摘要，模型上下文安全降级（不注入）；
    - ``degraded``：结构损坏或 schema 不兼容，不注入伪造内容。

    ``sections`` 只保留结构有效的条目；完全没有 id/label 的行会被拒绝并
    计入 ``dropped``。只有 id、没有 label 的行保留在数据中（供技术诊断与
    ``include_ids`` 用途），但不会进入默认的玩家安全摘要，绝不臆造内容。
    """

    status: str
    sections: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    author_prose: dict[str, Any] = field(default_factory=dict)
    dropped: dict[str, int] = field(default_factory=dict)
    reason: str = ""


def structural_issues(raw: Any) -> list[str]:
    """校验结构契约；返回面向开发者/管理员的中文技术问题列表。

    只检查 schema、区块类型与条目类型。行级内容缺失（例如实体缺少标签）
    不属于结构损坏，由运行时安全忽略并计入诊断，避免把可用世界一刀切阻断。
    """

    issues: list[str] = []
    if not isinstance(raw, Mapping):
        return [f"{RULES_DIGEST_PATH} 根节点必须是对象"]
    schema = raw.get("schema")
    if schema != RULES_DIGEST_SCHEMA:
        issues.append(
            f"{RULES_DIGEST_PATH} schema 不兼容：{schema!r}（需要 {RULES_DIGEST_SCHEMA}）"
        )
    for key in _SECTION_KEYS:
        value = raw.get(key)
        if value is None:
            continue
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            issues.append(f"{RULES_DIGEST_PATH}.{key} 必须是数组")
            continue
        for index, item in enumerate(value):
            if not isinstance(item, Mapping):
                issues.append(
                    f"{RULES_DIGEST_PATH}.{key}[{index}] 必须是对象"
                )
    prose = raw.get("author_prose")
    if prose is not None and not isinstance(prose, Mapping):
        issues.append(f"{RULES_DIGEST_PATH}.author_prose 必须是对象")
    return issues


def _entity_entry(item: Mapping[str, Any]) -> dict[str, str] | None:
    entry = {
        "id": str(item.get("id") or "").strip(),
        "label": str(item.get("label") or "").strip(),
    }
    if not entry["id"] and not entry["label"]:
        return None
    return entry


def _capability_entry(item: Mapping[str, Any]) -> dict[str, Any] | None:
    entry = {
        "id": str(item.get("id") or "").strip(),
        "label": str(item.get("label") or "").strip(),
    }
    if not entry["id"] and not entry["label"]:
        return None
    for key in ("boundaries", "limitations"):
        value = item.get(key)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            entry[key] = list(value)
        else:
            entry[key] = []
    return entry


def _track_entry(item: Mapping[str, Any]) -> dict[str, Any] | None:
    track_id = str(item.get("track_id") or item.get("id") or "").strip()
    levels = item.get("levels")
    if not track_id or not isinstance(levels, int) or levels < 1:
        return None
    return {
        "track_id": track_id,
        "label": str(item.get("label") or "").strip(),
        "levels": levels,
    }


def load_rules_digest(world: Mapping[str, Any] | None) -> RulesDigestState:
    """从编译世界加载并整理摘要。

    - 未附带摘要：``absent``，不注入、不报错；
    - 结构损坏或 schema 不兼容：``degraded``，不注入伪造内容，
      原因写入 ``reason`` 供开发者/管理员排查；
    - 结构有效：``ready``，无意义条目被忽略并计入 ``dropped``。
    """

    raw = (world or {}).get("rules_digest")
    if raw is None:
        return RulesDigestState(
            "absent",
            reason=(
                f"世界包未包含 {RULES_DIGEST_PATH}，模型上下文未注入"
                "权威规则摘要（D1-DATA-010）；已安全降级，不伪造内容"
            ),
        )
    issues = structural_issues(raw)
    if not isinstance(raw, Mapping):
        return RulesDigestState("degraded", reason="；".join(issues))
    if raw.get("schema") != RULES_DIGEST_SCHEMA:
        # schema 不兼容时不按旧结构解析，避免把新版本内容错误注入。
        return RulesDigestState("degraded", reason="；".join(issues))
    sections: dict[str, list[dict[str, Any]]] = {}
    dropped: dict[str, int] = {}
    for key in _SECTION_KEYS:
        value = raw.get(key)
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            continue
        entries: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, Mapping):
                continue
            if key in _ENTITY_SECTIONS:
                normalized = _entity_entry(item)
            elif key == "capability_boundaries":
                normalized = _capability_entry(item)
            else:
                normalized = _track_entry(item)
            if normalized is None:
                dropped[key] = dropped.get(key, 0) + 1
            else:
                entries.append(normalized)
        if entries:
            sections[key] = entries
    prose = raw.get("author_prose")
    author_prose = dict(prose) if isinstance(prose, Mapping) else {}
    if issues:
        return RulesDigestState(
            "degraded",
            sections,
            author_prose,
            dropped,
            "；".join(issues),
        )
    return RulesDigestState("ready", sections, author_prose, dropped, "")


def build_rules_digest_block(
    state: RulesDigestState,
    *,
    include_ids: bool = False,
) -> str:
    """把整理后的摘要压缩为模型系统上下文可用的内部规则摘要。

    默认只输出中文标签（``include_ids=False``），避免模型把内部稳定 ID
    带进玩家可见叙事；``include_ids=True`` 供技术诊断或需要稳定引用的
    内部用途。空摘要返回空字符串，调用方据此跳过注入。
    """

    sections: dict[str, Any] = {}
    for key in _SECTION_KEYS:
        entries = state.sections.get(key)
        if not entries:
            continue
        label = _SECTION_LABELS[key]
        if key in _ENTITY_SECTIONS:
            names: list[str] = []
            for entry in entries:
                name = entry["label"]
                if include_ids and entry["id"] and entry["id"] != name:
                    name = f"{name}（{entry['id']}）" if name else entry["id"]
                if name:
                    names.append(name)
            if names:
                sections[label] = names
        elif key == "capability_boundaries":
            items: list[dict[str, Any]] = []
            for entry in entries:
                name = entry["label"]
                if include_ids and entry["id"] and entry["id"] != name:
                    name = f"{name}（{entry['id']}）" if name else entry["id"]
                if not name:
                    continue
                item: dict[str, Any] = {"能力": name}
                if entry.get("boundaries"):
                    item["边界"] = list(entry["boundaries"])
                if entry.get("limitations"):
                    item["限制"] = list(entry["limitations"])
                items.append(item)
            if items:
                sections[label] = items
        elif key == "ability_tracks":
            tracks: list[dict[str, Any]] = []
            for entry in entries:
                # 摘要格式中轨迹没有独立 label，track_id 即其权威名称；
                # 该块只进入模型内部上下文，不进入玩家可见渠道。
                name = entry.get("label") or entry.get("track_id") or ""
                if not name:
                    continue
                tracks.append({"轨迹": name, "等级数": entry.get("levels", 0)})
            if tracks:
                sections[label] = tracks
    if state.author_prose:
        sections["作者规则"] = state.author_prose
    if not sections:
        return ""
    return json.dumps(
        sections,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def rules_digest_diagnostics(state: RulesDigestState) -> dict[str, Any]:
    """面向开发者/管理员的技术诊断；不含玩家可见文案。"""

    unrenderable: dict[str, int] = {}
    for key, entries in state.sections.items():
        if key not in _ENTITY_SECTIONS and key != "capability_boundaries":
            continue
        count = sum(1 for entry in entries if not entry.get("label"))
        if count:
            unrenderable[key] = count
    return {
        "status": state.status,
        "reason": state.reason,
        "sections": {
            key: len(state.sections.get(key) or []) for key in _SECTION_KEYS
        },
        "dropped": dict(state.dropped),
        "unrenderable": unrenderable,
        "author_prose_keys": sorted(state.author_prose.keys()),
    }


__all__ = [
    "RULES_DIGEST_PATH",
    "RULES_DIGEST_SCHEMA",
    "RulesDigestState",
    "build_rules_digest_block",
    "load_rules_digest",
    "rules_digest_diagnostics",
    "structural_issues",
]
