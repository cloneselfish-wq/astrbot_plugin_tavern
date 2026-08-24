from .common import *
from .system import *
from .context import *
from .planning import *
from .resolution import *


def _narrative_contract_fragment(original_prompt: str) -> str:
    """Keep the small speaker allowlist contract available during repair."""

    opening = "<narrative_document_contract>"
    closing = "</narrative_document_contract>"
    start = str(original_prompt or "").find(opening)
    if start < 0:
        return ""
    end = str(original_prompt).find(closing, start)
    if end < 0:
        return ""
    end += len(closing)
    return str(original_prompt)[start:end]


def repair_prompt(
    raw_output: str,
    error: str,
    original_prompt: str,
) -> str:
    """Request one complete schema-valid resolution repair.

    RC8 never accepts a prose-only replacement.  The repaired response must
    include its full NarrativeDocument and is parsed and fact-checked again by
    the same resolution contract before commit.
    """

    narrative_contract = _narrative_contract_fragment(original_prompt)
    return (
        "上一份输出无法通过校验。只修复 JSON 结构、字段类型、"
        "NarrativeDocument 结构、选项长度与行动角色归属；不得改变"
        "检定结论、世界状态变化、代价或记忆事实，也不要解释错误。"
        "可选 tone 只能填写小写 ASCII 安全标签（如 tense、calm、urgent），"
        "不确定或不合规时必须填写空字符串。"
        "非对白块的 speaker 必须为 null；对白或反应块的 speaker.actor_ref "
        "与 label 必须逐字复制下方 allowed_speakers，禁止留空或自造。"
        "返回单个完整 JSON 对象；不得只返回纯文本正文。\n\n"
        f"{narrative_contract}\n"
        f"<validation_error>{json.dumps(error, ensure_ascii=False)}</validation_error>\n"
        "<invalid_output>\n"
        f"{raw_output[:12000]}\n"
        "</invalid_output>\n"
    )


def choice_system_prompt(world: Mapping[str, Any]) -> str:
    """Small system prompt used only for A-D generation and repair."""

    return (
        f"{CORE_NARRATOR_RULES}\n\n"
        "你的当前任务仅是生成下一位角色的 A、B、C、D 四个行动选项。"
        "不要续写故事，不要输出状态补丁、记忆或骰点结果。\n\n"
        "每个非安全选项必须使用 check、automatic_consequence 或 vote_only；"
        "风险标签不能单独存在。自动后果必须填写 known_consequences，"
        "vote_only 必须 collective=true。\n\n"
        "<required_output_schema>\n"
        f"{_json(_CHOICE_SCHEMA)}\n"
        "</required_output_schema>\n"
    )


__all__ = [name for name in globals() if not name.startswith("__")]
