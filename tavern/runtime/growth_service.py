"""D1 技能成长纯服务（ability_track@1.0）。

对应 docs/D1_PLAN/17_SKILL_GROWTH_SYSTEM.md：技能等级提升必须同时改变
名称、效果边界、成本与限制，禁止只改 ``level`` 数字。

本模块不读写数据库：宿主在事务内调用这些纯函数，并把结果（成长状态、
预览、升级记录）持久化。所有玩家可见文案在此生成，普通视图不包含
track_id、effect key 或 schema 等内部契约。

规则来源按优先级：

- 世界包 ``rules.progression.growth_policy`` 为权威规则（证据数、里程碑
  要求、玩家确认、传奇级核对）；
- 每条 ``ability_track`` 的 ``upgrade_requirements`` 覆盖全局默认值；
- 等级数据只使用世界包声明的字段（name/summary/costs/effects/
  limitations/unlock_conditions）；未声明的影响维度（召唤物、物品、
  时钟、任务、命令等）一律标记「未声明」，不虚构。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


#: D1 等级定位标签（文档 17 §3 的默认四阶；仅用于 1~4 级展示）。
POSITION_LABELS = {
    1: "初识",
    2: "熟练",
    3: "精通",
    4: "传奇",
}

#: 世界未声明时使用的成长规则默认值（与 17 §5 一致）。
DEFAULT_POLICY: dict[str, Any] = {
    "enabled": True,
    "evidence_rule": {
        "evidence_min": 2,
        "milestone_required": True,
        "player_confirmation": True,
    },
    "legendary_rule": {
        "campaign_task_required": True,
        "gate_check": "dm_or_system",
        "permanent_cost_required": True,
    },
    "maximum_level": 4,
}

#: 等级数据未声明的影响维度；缺少数据时如实标记「未声明」。
UNDECLARED_IMPACT_DIMENSIONS = (
    "召唤物",
    "物品",
    "时钟",
    "任务",
    "可用命令",
    "持续时长",
    "失败后果",
    "AI 使用边界",
)


class GrowthError(ValueError):
    """技能成长操作被拒绝（文案为玩家可见中文，不含内部 ID）。"""

    def __init__(
        self,
        message: str,
        *,
        code: str = "growth.rejected",
    ) -> None:
        super().__init__(str(message))
        self.code = str(code or "growth.rejected")


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def _text(value: Any, max_chars: int = 2000) -> str:
    return str(value or "").strip()[:max_chars]


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError, OverflowError):
        return default


def _progression_module(world: Mapping[str, Any]) -> dict[str, Any]:
    rules = world.get("rules")
    rules = rules if isinstance(rules, Mapping) else {}
    module = rules.get("progression")
    if not isinstance(module, Mapping):
        module = world.get("progression")
    return module if isinstance(module, Mapping) else {}


def growth_policy(world: Mapping[str, Any]) -> dict[str, Any]:
    """读取世界成长规则；未声明 progression 模块时视为禁用成长。"""

    module = _progression_module(world)
    if not module:
        return dict(DEFAULT_POLICY, enabled=False)
    policy = _mapping(module.get("growth_policy"))
    evidence_rule = dict(DEFAULT_POLICY["evidence_rule"])
    evidence_rule.update(_mapping(policy.get("evidence_rule")))
    legendary_rule = dict(DEFAULT_POLICY["legendary_rule"])
    legendary_rule.update(_mapping(policy.get("legendary_rule")))
    return {
        "enabled": bool(policy.get("enabled", True)),
        "evidence_rule": evidence_rule,
        "legendary_rule": legendary_rule,
        "maximum_level": _int(
            policy.get("maximum_level"),
            _int(DEFAULT_POLICY["maximum_level"], 4),
        ),
        "minimum_signature_tracks": _int(
            policy.get("minimum_signature_tracks"),
            0,
        ),
        "note": _text(policy.get("note"), 500),
    }


def growth_enabled(world: Mapping[str, Any]) -> bool:
    return bool(growth_policy(world).get("enabled"))


def list_ability_tracks(world: Mapping[str, Any]) -> list[dict[str, Any]]:
    """列出世界声明的全部技能轨迹（保持作者源顺序）。"""

    module = _progression_module(world)
    raw = module.get("ability_tracks")
    tracks: list[dict[str, Any]] = []
    for item in _sequence(raw):
        if not isinstance(item, Mapping):
            continue
        track = dict(item)
        if not str(track.get("track_id") or track.get("id") or "").strip():
            continue
        tracks.append(track)
    return tracks


def find_ability_track(
    world: Mapping[str, Any],
    track_ref: str,
) -> dict[str, Any] | None:
    ref = str(track_ref or "").strip()
    for track in list_ability_tracks(world):
        if str(track.get("track_id") or track.get("id") or "") == ref:
            return dict(track)
    return None


def level_by_number(track: Mapping[str, Any], level: int) -> dict[str, Any] | None:
    for item in _sequence(track.get("levels")):
        if not isinstance(item, Mapping):
            continue
        if _int(item.get("level")) == _int(level):
            return dict(item)
    return None


def track_maximum_level(
    track: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> int:
    declared = _int(track.get("maximum_level"))
    if declared > 0:
        return declared
    return _int(policy.get("maximum_level"), 4)


def snapshot_of(level: Mapping[str, Any]) -> dict[str, Any]:
    """等级快照：只包含世界包声明的玩家可见字段。"""

    level = _mapping(level)
    costs = [
        _text(item, 500)
        for item in _sequence(level.get("costs"))
        if _text(item, 500)
    ]
    effects = [
        _text(item, 500)
        for item in _sequence(level.get("effects"))
        if _text(item, 500)
    ]
    limitations = [
        _text(item, 500)
        for item in _sequence(level.get("limitations"))
        if _text(item, 500)
    ]
    unlock_conditions = [
        _text(item, 500)
        for item in _sequence(level.get("unlock_conditions"))
        if _text(item, 500)
    ]
    return {
        "name": _text(level.get("name"), 80),
        "summary": _text(level.get("summary"), 1000),
        "costs": costs,
        "effects": effects,
        "limitations": limitations,
        "unlock_conditions": unlock_conditions,
    }


def default_growth(track: Mapping[str, Any]) -> dict[str, Any]:
    """未记录成长时的初始状态（以轨道 initial_level 为准）。"""

    current = _int(track.get("initial_level"), 1)
    current_level = level_by_number(track, current)
    snapshot = snapshot_of(current_level) if current_level else {}
    return {
        "revision": 0,
        "level": current,
        "level_name": snapshot.get("name") or "",
        "snapshot": snapshot,
        "evidence": [],
        "milestones": [],
        "history": [],
        "pending": None,
    }


def normalize_growth(
    state: Mapping[str, Any],
    track: Mapping[str, Any],
) -> dict[str, Any]:
    """把存储的 growth 子对象规整为完整结构（缺省回退初始状态）。"""

    raw = _mapping(state.get("growth"))
    base = default_growth(track)
    if not raw:
        return base
    snapshot = _mapping(raw.get("snapshot"))
    if not snapshot.get("name"):
        current_level = level_by_number(track, _int(raw.get("level"), base["level"]))
        snapshot = snapshot_of(current_level) if current_level else {}
    pending = raw.get("pending")
    pending = dict(pending) if isinstance(pending, Mapping) else None
    history = [
        dict(item)
        for item in _sequence(raw.get("history"))
        if isinstance(item, Mapping)
    ]
    return {
        "revision": _int(raw.get("revision"), base["revision"]),
        "level": _int(raw.get("level"), base["level"]),
        "level_name": _text(raw.get("level_name"), 80)
        or snapshot.get("name", ""),
        "snapshot": snapshot,
        "evidence": [
            dict(item)
            for item in _sequence(raw.get("evidence"))
            if isinstance(item, Mapping)
        ],
        "milestones": [
            dict(item)
            for item in _sequence(raw.get("milestones"))
            if isinstance(item, Mapping)
        ],
        "history": history,
        "pending": pending,
    }


def evidence_counts(growth: Mapping[str, Any]) -> tuple[int, int]:
    evidence = [
        item
        for item in _sequence(growth.get("evidence"))
        if isinstance(item, Mapping)
    ]
    milestones = [
        item
        for item in _sequence(growth.get("milestones"))
        if isinstance(item, Mapping)
    ]
    return len(evidence), len(milestones)


def threshold_issues(
    growth: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> list[str]:
    """升级候选门槛（17 §5）：至少 N 个证据 + 里程碑 + 玩家确认。"""

    issues: list[str] = []
    evidence_rule = _mapping(policy.get("evidence_rule"))
    evidence_min = _int(evidence_rule.get("evidence_min"), 2)
    milestone_required = bool(evidence_rule.get("milestone_required", True))
    evidence_count, milestone_count = evidence_counts(growth)
    if evidence_count < evidence_min:
        issues.append(f"成长证据不足（{evidence_count}/{evidence_min}）")
    if milestone_required and milestone_count < 1:
        issues.append("缺少职业或专精里程碑")
    return issues


def build_pending(
    growth: Mapping[str, Any],
    track: Mapping[str, Any],
    policy: Mapping[str, Any],
    now: str = "",
) -> dict[str, Any] | None:
    """达到阈值后生成待确认预览；不自动升级。

    - 已存在同目标预览时原样返回（保留创建时间）；
    - 已确认或已达最高等级时返回 None。
    """

    current = _int(growth.get("level"), _int(track.get("initial_level"), 1))
    target = current + 1
    maximum = track_maximum_level(track, policy)
    if target > maximum:
        return None
    target_level = level_by_number(track, target)
    if target_level is None:
        return None
    pending = growth.get("pending")
    if isinstance(pending, Mapping):
        pending_state = str(pending.get("state") or "preview")
        if pending_state == "confirmed":
            return None
        if _int(pending.get("target_level")) == target:
            return dict(pending)
    if threshold_issues(growth, policy):
        return None
    return {
        "target_level": target,
        "target_name": _text(target_level.get("name"), 80),
        "created_at": str(now or ""),
        "state": "preview",
    }


def validate_confirm(
    growth: Mapping[str, Any],
    track: Mapping[str, Any],
    policy: Mapping[str, Any],
    pending: Mapping[str, Any] | None,
    *,
    authority_confirm: bool = False,
) -> None:
    """确认前的全部校验；不满足时抛出带明确文案的 GrowthError。"""

    if pending is None:
        raise GrowthError(
            "当前没有待确认的升级预览；先积累成长证据与里程碑，"
            "再发送「成长 确认」。",
            code="growth.no_pending",
        )
    pending_state = str(pending.get("state") or "preview")
    if pending_state != "preview":
        raise GrowthError(
            "该升级已经确认过，无需重复确认。",
            code="growth.already_confirmed",
        )
    current = _int(growth.get("level"), _int(track.get("initial_level"), 1))
    target = _int(pending.get("target_level"))
    maximum = track_maximum_level(track, policy)
    if target > maximum:
        raise GrowthError(
            "该技能已达到最高等级，无法继续升级。",
            code="growth.max_level",
        )
    if target != current + 1:
        raise GrowthError(
            "不能跳级升级；请按等级顺序逐级确认。",
            code="growth.skip_level",
        )
    if level_by_number(track, target) is None:
        raise GrowthError(
            "世界未声明该等级的技能数据，暂时无法升级。",
            code="growth.undeclared_level",
        )
    issues = threshold_issues(growth, policy)
    if issues:
        raise GrowthError(
            "升级条件未满足：" + "；".join(issues) + "。",
            code="growth.threshold_unmet",
        )
    if target >= 4:
        legendary_rule = _mapping(policy.get("legendary_rule"))
        if str(legendary_rule.get("gate_check") or "dm_or_system") in {
            "dm_or_system",
            "dm",
        } and not authority_confirm:
            raise GrowthError(
                "传奇级需要主持或系统完成条件核对后才能确认。",
                code="growth.legendary_gate",
            )


def apply_upgrade(
    growth: Mapping[str, Any],
    track: Mapping[str, Any],
    policy: Mapping[str, Any],
    pending: Mapping[str, Any],
    *,
    operation_id: str,
    confirmed_at: str = "",
) -> dict[str, Any]:
    """应用一次已确认的升级，返回新的 growth 状态（纯构造）。"""

    current = _int(growth.get("level"), _int(track.get("initial_level"), 1))
    target = _int(pending.get("target_level"))
    target_level = level_by_number(track, target)
    if target_level is None:
        raise GrowthError(
            "世界未声明该等级的技能数据，无法完成升级。",
            code="growth.undeclared_level",
        )
    old_name = _text(growth.get("level_name"), 80) or ""
    snapshot = snapshot_of(target_level)
    history = [
        dict(item)
        for item in _sequence(growth.get("history"))
        if isinstance(item, Mapping)
    ]
    history.append(
        {
            "from_level": current,
            "to_level": target,
            "from_name": old_name,
            "to_name": snapshot.get("name") or "",
            "confirmed_at": str(confirmed_at or ""),
            "operation_id": str(operation_id or ""),
        }
    )
    return {
        "revision": _int(growth.get("revision"), 0) + 1,
        "level": target,
        "level_name": snapshot.get("name") or "",
        "snapshot": snapshot,
        "evidence": [
            dict(item)
            for item in _sequence(growth.get("evidence"))
            if isinstance(item, Mapping)
        ],
        "milestones": [
            dict(item)
            for item in _sequence(growth.get("milestones"))
            if isinstance(item, Mapping)
        ],
        "history": history,
        "pending": {
            "target_level": target,
            "target_name": snapshot.get("name") or "",
            "created_at": str(pending.get("created_at") or ""),
            "state": "confirmed",
            "confirmed_at": str(confirmed_at or ""),
            "operation_id": str(operation_id or ""),
        },
    }


def resolve_track_ordinal(
    tracks: Sequence[Mapping[str, Any]],
    ordinal: Any,
) -> str:
    """按当前页序号解析技能轨迹（玩家不输入内部稳定 ID）。"""

    index = _int(ordinal) - 1
    if index < 0 or index >= len(tracks):
        raise GrowthError(
            f"技能序号超出范围（当前共 {len(tracks)} 项），"
            "请使用页面上的序号。",
            code="growth.bad_ordinal",
        )
    track = tracks[index]
    return str(track.get("track_id") or track.get("id") or "")


def declared_impacts(level: Mapping[str, Any]) -> list[dict[str, str]]:
    """等级影响声明：世界数据未声明时如实标记「未声明」。"""

    impacts: list[dict[str, str]] = []
    for dimension in UNDECLARED_IMPACT_DIMENSIONS:
        impacts.append(
            {
                "dimension": dimension,
                "status": "未声明",
                "message": f"世界包未声明本次升级对{dimension}的影响。",
            }
        )
    return impacts


def position_label(level: int) -> str:
    return POSITION_LABELS.get(_int(level), "")


__all__ = [
    "DEFAULT_POLICY",
    "GrowthError",
    "POSITION_LABELS",
    "UNDECLARED_IMPACT_DIMENSIONS",
    "apply_upgrade",
    "build_pending",
    "declared_impacts",
    "default_growth",
    "evidence_counts",
    "find_ability_track",
    "growth_enabled",
    "growth_policy",
    "level_by_number",
    "list_ability_tracks",
    "normalize_growth",
    "position_label",
    "resolve_track_ordinal",
    "snapshot_of",
    "threshold_issues",
    "track_maximum_level",
    "validate_confirm",
]
