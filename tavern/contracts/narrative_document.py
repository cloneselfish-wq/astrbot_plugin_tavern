"""Pure RC8 NarrativeDocument contract.

The module owns one canonical DTO/schema for model parsing, quality checks,
persistence hashes, BOT text and public projections. It performs no I/O.
Pre-RC8 legacy text stays an explicit text-only DTO and is never reverse-parsed.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .common import contains_leakage

NARRATIVE_DOCUMENT_SCHEMA_ID = "tavern-narrative-document/1.0.0"
NARRATIVE_DOCUMENT_SCHEMA = NARRATIVE_DOCUMENT_SCHEMA_ID
NARRATIVE_BLOCK_KINDS = frozenset(
    {"narration", "action", "dialogue", "reaction", "transition", "reveal", "system_note"}
)
NARRATIVE_MODES = frozenset({"minimal", "balanced", "epic"})
KNOWN_NARRATIVE_VISIBILITIES = frozenset({"public", "host", "private"})
PUBLIC_NARRATIVE_VISIBILITIES = frozenset({"public"})
_TONE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

NARRATIVE_DOCUMENT_OUTPUT_SCHEMA: dict[str, Any] = {
    "schema": NARRATIVE_DOCUMENT_SCHEMA_ID,
    "mode": "minimal | balanced | epic",
    "title": "可选的本轮短标题",
    "blocks": [{
        "kind": "narration | action | dialogue | reaction | transition | reveal | system_note",
        "text": "单一叙事功能的玩家可见文本，8-500 个可见字符",
        "speaker": (
            "narration/action/transition/reveal/system_note 必须为 null；"
            "dialogue/reaction 才可使用对象，对象中的 actor_ref 与 label "
            "必须逐字复制本轮 allowed_speakers 对应项，禁止留空或自造"
        ),
        "tone": "可选；填空字符串，或填写 1-32 位小写 ASCII 安全标签（^[a-z][a-z0-9_-]{0,31}$），例如 tense、calm、urgent；不得使用中文、空格或大写字母",
        "visibility": "public",
        "quoted_input": "仅逐字引用本轮玩家原始输入时为 true",
    }],
    "continuity": {
        "scene_changed": "布尔值",
        "time_advanced": "布尔值",
        "revealed_fact_count": "非负整数",
    },
}


@dataclass(frozen=True, slots=True)
class NarrativeModeBounds:
    mode: str
    minimum: int
    maximum: int
    block_minimum: int
    block_maximum: int
    dialogue_minimum: int
    dialogue_maximum: int

    def effective_maximum(
        self, *, opening: bool = False, max_output_chars: int | None = None
    ) -> int:
        maximum = int(self.maximum * 1.25) if opening else self.maximum
        return min(maximum, max(0, int(max_output_chars))) if max_output_chars is not None else maximum


NARRATIVE_MODE_BOUNDS: Mapping[str, NarrativeModeBounds] = {
    "minimal": NarrativeModeBounds("minimal", 350, 600, 4, 7, 0, 3),
    "balanced": NarrativeModeBounds("balanced", 700, 1200, 7, 12, 2, 6),
    "epic": NarrativeModeBounds("epic", 1400, 2600, 12, 22, 4, 10),
}


class NarrativeContractError(ValueError):
    """Deterministic contract failure with a machine-safe code and path."""

    def __init__(self, code: str, message: str, path: str = "") -> None:
        super().__init__(message)
        self.code = str(code)
        self.path = str(path)


class NarrativeRepairError(NarrativeContractError):
    """A structure-only repair cannot be proven safe."""


def _fail(code: str, message: str, path: str = "") -> None:
    raise NarrativeContractError(code, message, path)


def _need(condition: bool, code: str, message: str, path: str = "") -> None:
    if not condition:
        _fail(code, message, path)


def _strict_keys(value: Mapping[str, Any], allowed: frozenset[str], path: str) -> None:
    unknown = sorted(str(key) for key in value if str(key) not in allowed)
    _need(not unknown, "field_unknown", f"{path or 'document'} 包含未知字段：{', '.join(unknown)}", path)


def _normalize_optional_tone(value: str) -> str:
    """Keep a safe presentation hint; discard invalid optional metadata."""

    normalized = value.strip().lower()
    return normalized if _TONE_RE.fullmatch(normalized) else ""


@dataclass(frozen=True, slots=True)
class NarrativeSpeaker:
    actor_ref: str
    label: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], *, path: str) -> "NarrativeSpeaker":
        _strict_keys(value, frozenset({"actor_ref", "label"}), path)
        actor_ref, label = value.get("actor_ref"), value.get("label")
        _need(isinstance(actor_ref, str) and bool(actor_ref.strip()), "speaker_ref_missing", "speaker.actor_ref 必须为非空文本", f"{path}.actor_ref")
        _need(isinstance(label, str) and bool(label.strip()), "speaker_label_missing", "speaker.label 必须为非空文本", f"{path}.label")
        return cls(actor_ref.strip(), label.strip())

    def to_dict(self) -> dict[str, str]:
        return {"actor_ref": self.actor_ref, "label": self.label}


@dataclass(frozen=True, slots=True)
class NarrativeBlock:
    kind: str
    text: str
    speaker: NarrativeSpeaker | None = None
    tone: str = ""
    visibility: str = "public"
    quoted_input: bool = False

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], *, path: str) -> "NarrativeBlock":
        _strict_keys(value, frozenset({"kind", "text", "speaker", "tone", "visibility", "quoted_input"}), path)
        kind, text = value.get("kind"), value.get("text")
        tone, visibility = value.get("tone", ""), value.get("visibility", "public")
        quoted = value.get("quoted_input", False)
        _need(isinstance(kind, str), "block_kind_invalid", "block.kind 必须为文本", f"{path}.kind")
        _need(isinstance(text, str), "block_text_invalid", "block.text 必须为文本", f"{path}.text")
        _need(isinstance(tone, str), "block_tone_invalid", "block.tone 必须为文本", f"{path}.tone")
        _need(isinstance(visibility, str), "visibility_invalid", "block.visibility 必须为文本", f"{path}.visibility")
        _need(isinstance(quoted, bool), "quoted_input_invalid", "block.quoted_input 必须为布尔值", f"{path}.quoted_input")
        raw_speaker = value.get("speaker")
        _need(raw_speaker is None or isinstance(raw_speaker, Mapping), "speaker_invalid", "block.speaker 必须为对象或 null", f"{path}.speaker")
        speaker = NarrativeSpeaker.from_mapping(raw_speaker, path=f"{path}.speaker") if isinstance(raw_speaker, Mapping) else None
        return cls(
            kind.strip().lower(),
            text.strip(),
            speaker,
            _normalize_optional_tone(tone),
            visibility.strip().lower(),
            quoted,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "text": self.text,
            "speaker": self.speaker.to_dict() if self.speaker else None,
            "tone": self.tone,
            "visibility": self.visibility,
            "quoted_input": self.quoted_input,
        }


@dataclass(frozen=True, slots=True)
class NarrativeContinuity:
    scene_changed: bool = False
    time_advanced: bool = False
    revealed_fact_count: int = 0

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], *, path: str = "continuity") -> "NarrativeContinuity":
        _strict_keys(value, frozenset({"scene_changed", "time_advanced", "revealed_fact_count"}), path)
        scene, time, count = value.get("scene_changed", False), value.get("time_advanced", False), value.get("revealed_fact_count", 0)
        _need(isinstance(scene, bool), "continuity_invalid", "continuity.scene_changed 必须为布尔值", f"{path}.scene_changed")
        _need(isinstance(time, bool), "continuity_invalid", "continuity.time_advanced 必须为布尔值", f"{path}.time_advanced")
        _need(isinstance(count, int) and not isinstance(count, bool) and count >= 0, "continuity_invalid", "continuity.revealed_fact_count 必须为非负整数", f"{path}.revealed_fact_count")
        return cls(scene, time, count)

    def to_dict(self) -> dict[str, Any]:
        return {"scene_changed": self.scene_changed, "time_advanced": self.time_advanced, "revealed_fact_count": self.revealed_fact_count}


@dataclass(frozen=True, slots=True)
class NarrativeDocument:
    schema: str
    mode: str
    title: str
    blocks: tuple[NarrativeBlock, ...]
    continuity: NarrativeContinuity

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "NarrativeDocument":
        _strict_keys(value, frozenset({"schema", "mode", "title", "blocks", "continuity"}), "document")
        schema, mode, title = value.get("schema"), value.get("mode"), value.get("title", "")
        raw_blocks, continuity = value.get("blocks"), value.get("continuity")
        _need(schema == NARRATIVE_DOCUMENT_SCHEMA_ID, "schema_invalid", f"schema 必须为 {NARRATIVE_DOCUMENT_SCHEMA_ID}", "schema")
        _need(isinstance(mode, str) and mode.strip().lower() in NARRATIVE_MODES, "mode_invalid", "mode 必须为 minimal、balanced 或 epic", "mode")
        _need(isinstance(title, str), "title_invalid", "title 必须为文本", "title")
        _need(isinstance(raw_blocks, Sequence) and not isinstance(raw_blocks, (str, bytes)), "blocks_invalid", "blocks 必须为数组", "blocks")
        blocks: list[NarrativeBlock] = []
        for index, raw in enumerate(raw_blocks):
            _need(isinstance(raw, Mapping), "block_invalid", "每个 block 必须为对象", f"blocks[{index}]")
            blocks.append(NarrativeBlock.from_mapping(raw, path=f"blocks[{index}]"))
        _need(isinstance(continuity, Mapping), "continuity_invalid", "continuity 必须为对象", "continuity")
        return cls(NARRATIVE_DOCUMENT_SCHEMA_ID, mode.strip().lower(), title.strip(), tuple(blocks), NarrativeContinuity.from_mapping(continuity))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "mode": self.mode,
            "title": self.title,
            "blocks": [block.to_dict() for block in self.blocks],
            "continuity": self.continuity.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class LegacyNarrativeText:
    """Explicit pre-RC8 fallback; intentionally has no blocks or speaker fields."""

    text: str
    label: str = "旧记录"
    legacy_record: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"legacy_record": True, "label": self.label, "text": self.text}


@dataclass(frozen=True, slots=True)
class NarrativeDeliveryPart:
    part_index: int
    total: int
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {"part_index": self.part_index, "total": self.total, "text": self.text}


_OPAQUE_REF_RE = re.compile(r"^[a-z][a-z0-9]{0,15}_[A-Za-z0-9_-]{8,64}$")
_HTML_RE = re.compile(r"<\s*/?\s*[a-zA-Z][^>]*>")
_MARKDOWN_LINK_RE = re.compile(r"\[[^\]\n]{1,200}\]\([^\)\n]{1,500}\)")
_SCRIPT_RE = re.compile(r"(?:javascript\s*:|<\s*script\b|on\w+\s*=)", re.I)
_DATABASE_RE = re.compile(r"\b(?:SELECT|INSERT|UPDATE|DELETE|DROP|ALTER|CREATE)\s+(?:FROM|INTO|TABLE|DATABASE|\*)", re.I)
_PLAYER_CONTROL_PATTERNS = (
    re.compile(r"(?:你|玩家|玩家角色)(?:已经|便|就|于是|最终|毫不犹豫地?)?(?:决定|答应|拒绝|同意|认为|觉得|感到|意识到|想要|说道|说出|说|开口|回答|走向|拿起|攻击|离开|跟随|服从|点头|摇头|转身|迈步)"),
    re.compile(r"(?:你|玩家|玩家角色)(?:的心中|心里)?(?:明白|知道|相信|怀疑|恐惧|愤怒|涌起|认定)"),
    re.compile(r"(?:替|代)(?:你|玩家|玩家角色)(?:作出|决定|选择|说出|行动)"),
)
_SCENE_PATCH_KEYS = frozenset({"location", "current_scene", "scene_ref"})
_TIME_PATCH_KEYS = frozenset({"time", "game_time", "world_time"})


def visible_narrative_length(value: Any) -> int:
    return len(re.sub(r"\s+", "", str(value or "")))


def _unsafe_reason(value: str) -> str:
    if any(ord(char) < 32 and char not in "\n\t\r" for char in value):
        return "包含控制字符"
    if _HTML_RE.search(value):
        return "包含 HTML"
    if _MARKDOWN_LINK_RE.search(value) or "```" in value:
        return "包含 Markdown 链接或围栏"
    if _SCRIPT_RE.search(value):
        return "包含脚本"
    if _DATABASE_RE.search(value):
        return "包含数据库语句"
    if contains_leakage(value):
        return "包含内部技术字段或稳定引用"
    return ""


def _finding(code: str, message: str, path: str = "") -> dict[str, str]:
    return {"level": "error", "code": code, "message": message, "path": path}


def _as_document(value: NarrativeDocument | Mapping[str, Any]) -> NarrativeDocument:
    if isinstance(value, NarrativeDocument):
        return value
    if isinstance(value, Mapping):
        return NarrativeDocument.from_mapping(value)
    _fail("document_invalid", "NarrativeDocument 必须为对象或 DTO", "document")


def _compact_quote(value: Any) -> str:
    text = re.sub(r"\s+", "", str(value or "")).strip()
    for left, right in (("“", "”"), ("「", "」"), ("『", "』"), ('"', '"')):
        if len(text) >= 2 and text.startswith(left) and text.endswith(right):
            return text[len(left) : -len(right)]
    return text


def _information_units(text: str) -> set[str]:
    compact = re.sub(r"\s+", "", text)
    cjk = "".join(re.findall(r"[\u3400-\u9fff]", compact))
    units = {cjk[index : index + 2] for index in range(max(0, len(cjk) - 1))}
    units.update(token.casefold() for token in re.findall(r"[A-Za-z0-9_]{2,}", compact))
    return units


def _overlap_ratio(current: str, previous: str) -> float:
    current_units, previous_units = _information_units(current), _information_units(previous)
    return len(current_units & previous_units) / max(1, len(current_units)) if current_units and previous_units else 0.0


def inspect_narrative_document(
    value: NarrativeDocument | Mapping[str, Any],
    *,
    dialogue_expected: bool = True,
    opening: bool = False,
    max_output_chars: int | None = None,
    allowed_speaker_refs: Sequence[str] | Mapping[str, str] | None = None,
    player_actor_refs: Sequence[str] = (),
    player_labels: Sequence[str] = (),
    player_input: str = "",
    allowed_visibilities: Sequence[str] = ("public",),
    require_opaque_speakers: bool = True,
    previous_narrative: str = "",
    state_patch: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Inspect without side effects; every unsafe condition fails closed."""

    try:
        document = _as_document(value)
    except NarrativeContractError as exc:
        return {"passed": False, "findings": [_finding(exc.code, str(exc), exc.path)], "visible_length": 0, "block_count": 0, "dialogue_count": 0}
    findings: list[dict[str, str]] = []
    add = findings.append
    policy = NARRATIVE_MODE_BOUNDS[document.mode]
    speaker_labels = (
        {str(key): str(label) for key, label in allowed_speaker_refs.items()}
        if isinstance(allowed_speaker_refs, Mapping)
        else None
    )
    allowed_speakers = set(speaker_labels) if speaker_labels is not None else ({str(item) for item in allowed_speaker_refs} if allowed_speaker_refs is not None else None)
    player_refs = {str(item) for item in player_actor_refs}
    player_names = tuple(str(item).strip() for item in player_labels if str(item).strip())
    visible_set = {str(item).strip().lower() for item in allowed_visibilities}
    if visible_narrative_length(document.title) > 80:
        add(_finding("title_too_long", "故事短标题不得超过 80 字", "title"))
    if document.title and (reason := _unsafe_reason(document.title)):
        add(_finding("unsafe_content", reason, "title"))

    story_texts: list[str] = []
    normalized_texts: list[str] = []
    dialogue_count = system_note_count = reveal_count = transition_count = 0
    for index, block in enumerate(document.blocks):
        path = f"blocks[{index}]"
        if block.kind not in NARRATIVE_BLOCK_KINDS:
            add(_finding("block_kind_invalid", "block.kind 不在允许集合", f"{path}.kind"))
        if not 8 <= visible_narrative_length(block.text) <= 500:
            add(_finding("block_length_invalid", "每个 block 必须为 8-500 个可见字符", f"{path}.text"))
        if reason := _unsafe_reason(block.text):
            add(_finding("unsafe_content", reason, f"{path}.text"))
        try:
            _safe_boundaries(block.text)
        except NarrativeContractError as exc:
            add(_finding(exc.code, str(exc), f"{path}.text"))
        if block.tone and not _TONE_RE.fullmatch(block.tone):
            add(_finding("tone_invalid", "tone 只能是简短小写安全标签", f"{path}.tone"))
        if block.visibility not in KNOWN_NARRATIVE_VISIBILITIES:
            add(_finding("visibility_invalid", "visibility 不在允许集合", f"{path}.visibility"))
        elif block.visibility not in visible_set:
            add(_finding("visibility_not_allowed", "当前 projection 不允许此可见性", f"{path}.visibility"))

        speaker, quoted_player = block.speaker, False
        speaker_ref = speaker.actor_ref if speaker else ""
        if block.kind == "dialogue":
            dialogue_count += 1
            if speaker is None:
                add(_finding("dialogue_speaker_missing", "dialogue block 必须包含合法 speaker", f"{path}.speaker"))
        elif speaker is not None and block.kind != "reaction":
            add(_finding("speaker_not_allowed", "只有 dialogue/reaction block 可以声明 speaker", f"{path}.speaker"))
        if speaker is not None:
            if require_opaque_speakers and not _OPAQUE_REF_RE.fullmatch(speaker.actor_ref):
                add(_finding("speaker_ref_not_opaque", "公开 speaker.actor_ref 必须为 opaque key", f"{path}.speaker.actor_ref"))
            if allowed_speakers is not None and speaker.actor_ref not in allowed_speakers:
                add(_finding("speaker_not_allowed", "speaker 不在本轮允许发言者名单", f"{path}.speaker.actor_ref"))
            if speaker_labels is not None and speaker.actor_ref in speaker_labels and speaker.label != speaker_labels[speaker.actor_ref]:
                add(_finding("speaker_label_mismatch", "speaker.label 与本轮 viewer-safe label 不一致", f"{path}.speaker.label"))
            if not speaker.label or visible_narrative_length(speaker.label) > 80:
                add(_finding("speaker_label_invalid", "speaker.label 必须为 1-80 个安全字符", f"{path}.speaker.label"))
            elif reason := _unsafe_reason(speaker.label):
                add(_finding("speaker_label_unsafe", reason, f"{path}.speaker.label"))
        if speaker_ref in player_refs:
            quoted_player = bool(block.kind == "dialogue" and block.quoted_input and _compact_quote(block.text) and _compact_quote(block.text) in _compact_quote(player_input))
            if not quoted_player:
                add(_finding("possible_player_control", "玩家对白只能逐字引用本轮输入并标记 quoted_input", path))
        elif block.quoted_input:
            add(_finding("quoted_input_unverifiable", "quoted_input 只能用于可核对的玩家输入", f"{path}.quoted_input"))
        if not quoted_player and any(pattern.search(block.text) for pattern in _PLAYER_CONTROL_PATTERNS):
            add(_finding("possible_player_control", "正文可能替玩家决定台词、思想或行动", f"{path}.text"))
        if not quoted_player and any(
            re.search(re.escape(name) + r"(?:已经|便|就|于是|最终)?(?:决定|答应|拒绝|同意|认为|觉得|感到|意识到|说|开口|回答|走向|拿起|攻击|离开|点头|摇头|转身|迈步)", block.text)
            for name in player_names
        ):
            add(_finding("possible_player_control", "正文可能替具名玩家角色决定台词、思想或行动", f"{path}.text"))

        if block.kind == "system_note":
            system_note_count += 1
        else:
            story_texts.append(block.text)
        reveal_count += int(block.kind == "reveal")
        transition_count += int(block.kind == "transition")
        normalized = re.sub(r"\s+", "", block.text)
        if normalized:
            if normalized_texts and normalized == normalized_texts[-1]:
                add(_finding("adjacent_block_repetition", "相邻 block 内容重复", f"{path}.text"))
            elif normalized in normalized_texts:
                add(_finding("block_repetition", "NarrativeDocument 存在重复 block", f"{path}.text"))
            normalized_texts.append(normalized)

    block_count = len(document.blocks)
    if not policy.block_minimum <= block_count <= policy.block_maximum:
        add(_finding("block_count_out_of_bounds", f"{document.mode} 模式必须包含 {policy.block_minimum}-{policy.block_maximum} 个 blocks", "blocks"))
    if system_note_count > 2:
        add(_finding("system_note_limit", "system_note 最多 2 块", "blocks"))
    if not story_texts:
        add(_finding("story_blocks_missing", "NarrativeDocument 缺少故事 blocks", "blocks"))

    story_text = "\n\n".join(story_texts)
    visible_length = visible_narrative_length(story_text)
    effective_maximum = policy.effective_maximum(opening=opening, max_output_chars=max_output_chars)
    if not policy.minimum <= visible_length <= effective_maximum:
        add(_finding("narrative_length_out_of_bounds", f"{document.mode} 正文必须为 {policy.minimum}—{effective_maximum} 个可见字符", "blocks"))
    dialogue_minimum = policy.dialogue_minimum if dialogue_expected else 0
    if not dialogue_minimum <= dialogue_count <= policy.dialogue_maximum:
        add(_finding("dialogue_count_out_of_bounds", f"当前场景对白目标为 {dialogue_minimum}-{policy.dialogue_maximum} 句", "blocks"))
    if document.continuity.revealed_fact_count != reveal_count:
        add(_finding("reveal_count_mismatch", "revealed_fact_count 与 reveal blocks 数量不一致", "continuity.revealed_fact_count"))
    if transition_count and not (document.continuity.scene_changed or document.continuity.time_advanced):
        add(_finding("transition_state_mismatch", "transition block 必须对应场景或时间变化", "continuity"))

    if state_patch is not None:
        scene_patch = any(state_patch.get(key) not in (None, "") for key in _SCENE_PATCH_KEYS)
        time_patch = any(state_patch.get(key) not in (None, "") for key in _TIME_PATCH_KEYS)
        if scene_patch != document.continuity.scene_changed:
            add(_finding("transition_state_mismatch", "scene_changed 与 state patch 不一致", "continuity.scene_changed"))
        if time_patch != document.continuity.time_advanced:
            add(_finding("transition_state_mismatch", "time_advanced 与 state patch 不一致", "continuity.time_advanced"))
        if (scene_patch or time_patch) and not transition_count:
            add(_finding("transition_block_missing", "场景或时间变化必须有 transition block", "blocks"))
    if visible_length >= policy.minimum and len(_information_units(story_text)) < max(12, visible_length // 45):
        add(_finding("low_information_density", "故事信息密度过低，可能重复描写凑长度", "blocks"))
    if previous_narrative and visible_length >= 80 and _overlap_ratio(story_text, str(previous_narrative)) > 0.86:
        add(_finding("previous_turn_overlap", "故事正文与上一轮高度重复", "blocks"))
    return {
        "passed": not findings,
        "findings": findings,
        "visible_length": visible_length,
        "minimum": policy.minimum,
        "maximum": effective_maximum,
        "block_count": block_count,
        "block_minimum": policy.block_minimum,
        "block_maximum": policy.block_maximum,
        "dialogue_count": dialogue_count,
        "dialogue_minimum": dialogue_minimum,
        "dialogue_maximum": policy.dialogue_maximum,
        "system_note_count": system_note_count,
        "text_sha256": narrative_text_sha256(document),
    }


def parse_narrative_document(payload: Mapping[str, Any], **inspection_options: Any) -> NarrativeDocument:
    document = NarrativeDocument.from_mapping(payload)
    report = inspect_narrative_document(document, **inspection_options)
    if not report["passed"]:
        first = report["findings"][0]
        raise NarrativeContractError(str(first["code"]), str(first["message"]), str(first.get("path") or ""))
    return document


def canonical_narrative_json(value: NarrativeDocument | Mapping[str, Any]) -> str:
    return json.dumps(_as_document(value).to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def narrative_document_sha256(value: NarrativeDocument | Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_narrative_json(value).encode("utf-8")).hexdigest()


def _dialogue_text(block: NarrativeBlock) -> str:
    assert block.speaker is not None
    body = block.text.strip()
    if not (len(body) >= 2 and (body[0], body[-1]) in {("“", "”"), ("「", "」"), ("『", "』")}):
        body = f"“{body}”"
    return f"「{block.speaker.label}」\n{body}"


def _strip_outer_quote(value: Any) -> str:
    text = str(value or "").strip()
    for left, right in (("“", "”"), ("「", "」"), ("『", "』"), ('"', '"')):
        if len(text) >= 2 and text.startswith(left) and text.endswith(right):
            return text[len(left) : -len(right)]
    return text


def render_narrative_block(block: NarrativeBlock) -> str:
    return _dialogue_text(block) if block.kind == "dialogue" and block.speaker else block.text.strip()


def narrative_document_to_plain_text(
    value: NarrativeDocument | LegacyNarrativeText | Mapping[str, Any],
    *,
    include_title: bool = False,
    include_system_notes: bool = True,
    allowed_visibilities: Sequence[str] = ("public",),
) -> str:
    """Render natural text without schema, block kind or actor-ref leakage."""

    if isinstance(value, LegacyNarrativeText):
        return value.text
    document = _as_document(value)
    visibility = {str(item).strip().lower() for item in allowed_visibilities}
    parts = [document.title] if include_title and document.title else []
    for block in document.blocks:
        if block.visibility in visibility and (include_system_notes or block.kind != "system_note"):
            if rendered := render_narrative_block(block):
                parts.append(rendered)
    return "\n\n".join(parts)


def project_public_narrative_document(
    value: NarrativeDocument | Mapping[str, Any],
) -> dict[str, Any]:
    """Return the player DTO without schema IDs or stable actor references."""

    document = _as_document(value)
    blocks: list[dict[str, Any]] = []
    for block in document.blocks:
        if block.visibility != "public":
            continue
        item: dict[str, Any] = {
            "kind": block.kind,
            "text": block.text,
            "tone": block.tone,
        }
        if block.speaker is not None:
            item["speaker_label"] = block.speaker.label
        blocks.append(item)
    return {
        "mode": document.mode,
        "title": document.title,
        "blocks": blocks,
        "continuity": document.continuity.to_dict(),
    }


def narrative_text_sha256(
    value: NarrativeDocument | LegacyNarrativeText | Mapping[str, Any], **options: Any
) -> str:
    return hashlib.sha256(narrative_document_to_plain_text(value, **options).encode("utf-8")).hexdigest()


def _fact_material(value: NarrativeDocument | Mapping[str, Any]) -> dict[str, Any]:
    source = value.to_dict() if isinstance(value, NarrativeDocument) else dict(value)
    raw_blocks = source.get("blocks")
    _need(isinstance(raw_blocks, Sequence) and not isinstance(raw_blocks, (str, bytes)), "repair_unsafe", "无法从损坏 blocks 证明事实未改变", "blocks")
    blocks: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_blocks):
        _need(isinstance(raw, Mapping), "repair_unsafe", "无法从损坏 block 证明事实未改变", f"blocks[{index}]")
        speaker = raw.get("speaker")
        blocks.append({
            "kind": str(raw.get("kind") or "").strip().lower(),
            "text": str(raw.get("text") or "").strip(),
            "speaker_ref": str(speaker.get("actor_ref") or "").strip() if isinstance(speaker, Mapping) else "",
            "quoted_input": bool(raw.get("quoted_input", False)),
        })
    continuity = source.get("continuity")
    _need(isinstance(continuity, Mapping), "repair_unsafe", "缺少 continuity 时无法证明事实未改变", "continuity")
    return {"blocks": blocks, "continuity": {key: continuity.get(key) for key in ("scene_changed", "time_advanced", "revealed_fact_count")}}


def narrative_fact_sha256(value: NarrativeDocument | Mapping[str, Any]) -> str:
    material = json.dumps(_fact_material(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def repair_narrative_document(payload: Mapping[str, Any], **inspection_options: Any) -> NarrativeDocument:
    """Repair schema/presentation defaults only; never rewrite facts or visibility."""

    if not isinstance(payload, Mapping):
        raise NarrativeRepairError("repair_unsafe", "repair 输入必须为对象", "document")
    try:
        before_hash = narrative_fact_sha256(payload)
    except NarrativeContractError as exc:
        raise NarrativeRepairError(exc.code, str(exc), exc.path) from exc
    source = dict(payload)
    allowed_top = frozenset({"schema", "mode", "title", "blocks", "continuity"})
    unknown = sorted(str(key) for key in source if str(key) not in allowed_top)
    if unknown:
        raise NarrativeRepairError("repair_unsafe", "repair 不会丢弃未知事实字段", f"document.{unknown[0]}")
    source["schema"] = NARRATIVE_DOCUMENT_SCHEMA_ID
    source.setdefault("title", "")
    if isinstance(source.get("mode"), str):
        source["mode"] = str(source["mode"]).strip().lower()
    raw_blocks = source.get("blocks")
    if not isinstance(raw_blocks, Sequence) or isinstance(raw_blocks, (str, bytes)):
        raise NarrativeRepairError("repair_unsafe", "blocks 无法安全修复", "blocks")
    allowed_block = frozenset({"kind", "text", "speaker", "tone", "visibility", "quoted_input"})
    repaired: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_blocks):
        if not isinstance(raw, Mapping):
            raise NarrativeRepairError("repair_unsafe", "block 无法安全修复", f"blocks[{index}]")
        item = dict(raw)
        unknown = sorted(str(key) for key in item if str(key) not in allowed_block)
        if unknown:
            raise NarrativeRepairError("repair_unsafe", "repair 不会丢弃未知 block 字段", f"blocks[{index}].{unknown[0]}")
        for key in ("kind", "text", "tone", "visibility"):
            if isinstance(item.get(key), str):
                if key == "text":
                    item[key] = str(item[key]).strip()
                elif key == "tone":
                    item[key] = _normalize_optional_tone(str(item[key]))
                else:
                    item[key] = str(item[key]).strip().lower()
        item.setdefault("tone", "")
        item.setdefault("quoted_input", False)
        if "visibility" not in item:
            raise NarrativeRepairError("repair_unsafe", "缺少 visibility 时不能猜测公开范围", f"blocks[{index}].visibility")
        raw_speaker = item.get("speaker")
        if (
            item.get("kind") != "dialogue"
            and isinstance(raw_speaker, Mapping)
            and not str(raw_speaker.get("actor_ref") or "").strip()
            and not str(raw_speaker.get("label") or "").strip()
        ):
            # Some providers materialize a nullable object as two empty
            # strings.  For a non-dialogue block this is exactly the same
            # narrative fact as null; dialogue still fails closed because its
            # speaker cannot be inferred safely.
            item["speaker"] = None
        if "speaker" not in item and item.get("kind") != "dialogue":
            item["speaker"] = None
        repaired.append(item)
    source["blocks"] = repaired
    try:
        document = parse_narrative_document(source, **inspection_options)
    except NarrativeContractError as exc:
        raise NarrativeRepairError(exc.code, str(exc), exc.path) from exc
    if before_hash != narrative_fact_sha256(document):
        raise NarrativeRepairError("repair_changed_facts", "repair 检测到事实变化", "document")
    return document


def legacy_text_fallback(value: Any, *, legacy_record: bool = False) -> LegacyNarrativeText:
    """Return explicit pre-RC8 text without inferring blocks, kind or speaker."""

    _need(legacy_record, "legacy_marker_required", "纯文本 fallback 必须明确标记为旧版记录", "legacy_record")
    text = str(value or "").strip()
    _need(bool(text), "legacy_text_empty", "旧记录正文为空", "text")
    _need(len(text) <= 12000, "legacy_text_too_long", "旧记录正文超过安全上限", "text")
    _need(not _unsafe_reason(text), "legacy_text_unsafe", "旧记录包含不可公开的技术或脚本内容", "text")
    return LegacyNarrativeText(text)


_QUOTE_PAIRS = {"“": "”", "「": "」", "『": "』", "‘": "’"}
_QUOTE_CLOSE = frozenset(_QUOTE_PAIRS.values())
_STRONG_BOUNDARIES = frozenset("。！？!?；;\n")
_WEAK_BOUNDARIES = frozenset("，,、 \t")


def _safe_boundaries(text: str) -> tuple[list[int], list[int]]:
    stack: list[str] = []
    strong: list[int] = []
    weak: list[int] = []
    ascii_quote = False
    for index, char in enumerate(text):
        if char in _QUOTE_PAIRS:
            stack.append(_QUOTE_PAIRS[char])
        elif char in _QUOTE_CLOSE:
            _need(bool(stack) and stack[-1] == char, "quote_unbalanced", "故事文本包含未配对引号", "blocks")
            stack.pop()
        elif char == '"':
            ascii_quote = not ascii_quote
        if not stack and not ascii_quote:
            if char in _STRONG_BOUNDARIES:
                strong.append(index + 1)
            elif char in _WEAK_BOUNDARIES:
                weak.append(index + 1)
    _need(not stack and not ascii_quote, "quote_unbalanced", "故事文本包含未闭合引号", "blocks")
    return strong, weak


def _split_text_safely(text: str, maximum: int) -> list[str]:
    value = text.strip()
    if len(value) <= maximum:
        _safe_boundaries(value)
        return [value] if value else []
    strong, weak = _safe_boundaries(value)
    fragments: list[str] = []
    start = 0
    while len(value) - start > maximum:
        ceiling = start + maximum
        candidates = [position for position in strong if start < position <= ceiling]
        if not candidates:
            candidates = [position for position in weak if start < position <= ceiling]
        _need(bool(candidates), "chunk_boundary_unavailable", "片段超过平台上限且没有不截断引号的安全边界", "blocks")
        cut = max(candidates)
        if fragment := value[start:cut].strip():
            fragments.append(fragment)
        start = cut
        while start < len(value) and value[start].isspace():
            start += 1
    if tail := value[start:].strip():
        fragments.append(tail)
    return fragments


def _block_units(block: NarrativeBlock, maximum: int) -> list[str]:
    rendered = render_narrative_block(block)
    if len(rendered) <= maximum:
        _safe_boundaries(rendered)
        return [rendered]
    if block.kind == "dialogue" and block.speaker:
        prefix, suffix = f"「{block.speaker.label}」\n“", "”"
        available = maximum - len(prefix) - len(suffix)
        _need(available >= 8, "chunk_limit_too_small", "平台上限不足以安全投递 speaker 与对白", "maximum")
        return [f"{prefix}{part}{suffix}" for part in _split_text_safely(_strip_outer_quote(block.text), available)]
    return _split_text_safely(rendered, maximum)


def chunk_narrative_document(
    value: NarrativeDocument | Mapping[str, Any],
    maximum: int,
    *,
    include_title: bool = True,
    include_system_notes: bool = True,
    allowed_visibilities: Sequence[str] = ("public",),
) -> tuple[NarrativeDeliveryPart, ...]:
    """Use block boundaries, then safe sentences; never hard-slice a quote."""

    limit = int(maximum)
    _need(limit >= 64, "chunk_limit_too_small", "BOT 单条上限不得小于 64", "maximum")
    document = _as_document(value)
    visibility = {str(item).strip().lower() for item in allowed_visibilities}
    units = _split_text_safely(f"《{document.title}》", limit) if include_title and document.title else []
    for block in document.blocks:
        if block.visibility in visibility and (include_system_notes or block.kind != "system_note"):
            units.extend(_block_units(block, limit))
    pages: list[str] = []
    current = ""
    for unit in units:
        candidate = f"{current}\n\n{unit}" if current else unit
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                pages.append(current)
            current = unit
    if current:
        pages.append(current)
    total = len(pages)
    return tuple(NarrativeDeliveryPart(index, total, page) for index, page in enumerate(pages, 1))


def narrative_document_from_plain_text(
    value: Any,
    *,
    mode: str,
    title: str = "",
) -> NarrativeDocument:
    """Create a strict manual document without inventing or padding facts.

    This boundary is for trusted DM/editor text. It only divides existing
    sentences or paragraphs into narration blocks; it never guesses speakers,
    transitions, reveals, or hidden visibility.
    """

    selected = str(mode or "").strip().lower()
    _need(selected in NARRATIVE_MODES, "mode_invalid", "正文模式无效", "mode")
    text = str(value or "").strip()
    _need(bool(text), "manual_text_empty", "故事正文为空", "text")
    _need(not _unsafe_reason(text), "manual_text_unsafe", "故事正文包含不安全内容", "text")
    bounds = NARRATIVE_MODE_BOUNDS[selected]
    visible = visible_narrative_length(text)
    _need(
        bounds.minimum <= visible <= bounds.maximum,
        "narrative_length_out_of_bounds",
        f"{selected} 模式正文必须为 {bounds.minimum}-{bounds.maximum} 个可见字符",
        "text",
    )
    raw_units = [
        item.strip()
        for item in re.split(r"(?<=[。！？!?；;])\s*|\n+", text)
        if item.strip()
    ]
    _need(
        len(raw_units) >= bounds.block_minimum,
        "manual_blocks_insufficient",
        f"请至少使用 {bounds.block_minimum} 个完整句子或段落",
        "text",
    )
    target = min(bounds.block_maximum, len(raw_units))
    grouped: list[str] = []
    start = 0
    for index in range(target):
        remaining_units = len(raw_units) - start
        remaining_groups = target - index
        take = max(1, remaining_units // remaining_groups)
        if remaining_units % remaining_groups:
            take += 1
        grouped.append("".join(raw_units[start : start + take]).strip())
        start += take
    _need(
        all(8 <= visible_narrative_length(item) <= 500 for item in grouped),
        "manual_block_length_invalid",
        "每个手工故事分段必须为 8-500 个可见字符，请调整句子或段落",
        "text",
    )
    payload = {
        "schema": NARRATIVE_DOCUMENT_SCHEMA_ID,
        "mode": selected,
        "title": str(title or "").strip(),
        "blocks": [
            {
                "kind": "narration",
                "text": item,
                "speaker": None,
                "tone": "",
                "visibility": "public",
                "quoted_input": False,
            }
            for item in grouped
        ],
        "continuity": {
            "scene_changed": False,
            "time_advanced": False,
            "revealed_fact_count": 0,
        },
    }
    return parse_narrative_document(payload, dialogue_expected=False)


__all__ = [
    "KNOWN_NARRATIVE_VISIBILITIES", "LegacyNarrativeText", "NARRATIVE_BLOCK_KINDS",
    "NARRATIVE_DOCUMENT_OUTPUT_SCHEMA", "NARRATIVE_DOCUMENT_SCHEMA",
    "NARRATIVE_DOCUMENT_SCHEMA_ID", "NARRATIVE_MODE_BOUNDS", "NARRATIVE_MODES",
    "NarrativeBlock", "NarrativeContinuity", "NarrativeContractError",
    "NarrativeDeliveryPart", "NarrativeDocument", "NarrativeModeBounds",
    "NarrativeRepairError", "NarrativeSpeaker", "PUBLIC_NARRATIVE_VISIBILITIES",
    "canonical_narrative_json", "chunk_narrative_document", "inspect_narrative_document",
    "legacy_text_fallback", "narrative_document_sha256", "narrative_document_to_plain_text",
    "narrative_document_from_plain_text",
    "narrative_fact_sha256", "narrative_text_sha256", "parse_narrative_document",
    "project_public_narrative_document",
    "render_narrative_block", "repair_narrative_document", "visible_narrative_length",
]
