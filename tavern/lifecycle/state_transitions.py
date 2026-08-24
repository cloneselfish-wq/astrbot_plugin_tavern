from __future__ import annotations

from .world_time import *
from .character_creation import *
from .validation import *
from .risk_resolution import *



def safe_exit_narrative(
    world: Mapping[str, Any],
    character_name: str,
    *,
    forced: bool = False,
) -> str:
    rules = world.get("rules")
    rules = rules if isinstance(rules, Mapping) else {}
    templates = rules.get("safe_exit_templates")
    if isinstance(templates, Sequence) and not isinstance(
        templates, (str, bytes)
    ):
        usable = [
            clean_text(item, max_chars=300)
            for item in templates
            if clean_text(item, max_chars=300)
        ]
        if usable:
            return usable[0].replace("{character}", character_name)
    if forced:
        return (
            f"{character_name}暂时离开了队伍，去处理一件无法拖延的私事。"
            "其余人保留了对方留下的联络线索，未来仍可能在合理时机重逢。"
        )
    return (
        f"{character_name}在确认眼前局势暂时稳定后，与众人作了简短告别。"
        "这段同行经历被完整保留，若条件允许，仍可循着旧线索重新会合。"
    )


# 选项字母对应的圆形字母 emoji，用于行动回合的视觉分区
_CHOICE_LETTER_EMOJI = {
    "A": "🅰️",
    "B": "🅱️",
    "C": "🅲️",
    "D": "🅳️",
    "E": "🅴️",
    "F": "🅵️",
}

# 全队行动候选的编号标识（不占用个人选项的 A—D 字母）。
_TEAM_NUMBER_LABELS = ("①", "②", "③", "④")


_RISK_DOT = {
    "safe": "🟢 安全",
    "controlled": "🟡 可控",
    "dangerous": "🟠 危险",
    "desperate": "🔴 绝境",
    "lethal": "☠️ 致命",
}


def _risk_dot(risk: str) -> str:
    return _RISK_DOT.get(str(risk or "").lower(), "⚠️ 风险资料缺失")


def format_choices(
    character_name: str,
    choices: Sequence[Mapping[str, Any]],
    *,
    rerolls_left: int = 1,
    trigger_prefix: str = "t",
) -> str:
    """按 玩家文案契约渲染行动选项。"""

    lines = [f"# 「{character_name}」的行动", "", "## 可选行动"]
    has_collective = False
    for item in choices:
        key = str(item.get("key") or "").upper()
        text = str(item.get("text") or "行动说明缺失").strip()
        collective = bool(item.get("collective"))
        has_collective = has_collective or collective
        lines.extend(
            [
                "",
                f"- **{key}　{text}**"
                + (" · 🔵 全队行动" if collective else ""),
            ]
        )
        resolution_kind = str(item.get("resolution_kind") or "none")
        risk_copy = _risk_dot(str(item.get("risk") or ""))
        if resolution_kind == "check" or item.get("requires_check"):
            label = str(item.get("check_label") or item.get("check_stat") or "能力").strip()
            try:
                difficulty = int(item.get("difficulty") or 0)
            except (TypeError, ValueError):
                difficulty = 0
            check_copy = f"{risk_copy} · 〈{label}〉检定"
            if difficulty:
                check_copy += f"，难度 {difficulty}"
            lines.append(f"  {check_copy}")
        elif resolution_kind == "vote_only":
            lines.append("  🔵 需要全队表决")
        elif resolution_kind == "automatic_consequence":
            lines.append(f"  {risk_copy} · 无需检定")
        else:
            lines.append(f"  {risk_copy}")
        consequence = str(item.get("known_consequences") or "").strip()
        if consequence:
            lines.append(f"  > 已知影响：{consequence}")

    prefix = str(trigger_prefix or "t").strip() or "t"
    lines.extend(
        [
            "",
            "## 怎么选择",
            "",
            f"> `{prefix} A`",
            "",
            f"也可以补充演绎，例如：`{prefix} B 低声试探`",
            "",
            f"重整选项（本回合剩余 {max(0, rerolls_left)} 次）：",
            "",
            "> `/团 重整选项`",
        ]
    )
    if has_collective:
        lines.extend(["", "全队行动会发起表决，不消耗个人行动机会。"])
    return "\n".join(lines)

def vote_result(
    *,
    eligible_count: int,
    ballots: Sequence[Mapping[str, Any]],
    option_keys: Sequence[str],
) -> dict[str, Any]:
    counts = {str(key): 0 for key in option_keys}
    for ballot in ballots:
        key = str(ballot.get("option_key") or "")
        if key in counts:
            counts[key] += 1
    cast_count = sum(counts.values())
    quorum = cast_count > eligible_count / 2
    winners = [
        key for key, count in counts.items()
        if cast_count and count > cast_count / 2
    ]
    return {
        "counts": counts,
        "cast_count": cast_count,
        "eligible_count": eligible_count,
        "quorum": quorum,
        "winner": winners[0] if quorum and len(winners) == 1 else "",
        "all_voted": cast_count >= eligible_count,
    }

__all__ = [name for name in globals() if not name.startswith('__')]
