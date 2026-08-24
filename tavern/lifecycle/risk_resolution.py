from __future__ import annotations

from .world_time import *
from .character_creation import *
from .validation import *


# Normalization owns this limit: it is applied before any choice rendering or
# state transition occurs.  Keeping it here also avoids a lifecycle package
# import cycle between risk_resolution and state_transitions.
MAX_TEAM_CHOICES = 2


def _normalize_risk_id(item: Mapping[str, Any]) -> str:
    raw = str(item.get("danger_id") or item.get("risk") or "").strip().lower()
    if not raw:
        raise ValueError("行动选项缺少 risk/danger_id")
    risk = _RISK_ALIASES.get(raw)
    if risk is None:
        raise ValueError(f"未知危险度：{raw}")
    return risk


def normalize_choices(value: Any, world: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("行动选项必须是数组")
    contract = world_contract(world) if world is not None else None
    by_key = {}
    for item in value:
        if not isinstance(item, Mapping): continue
        key = str(item.get("key") or "").strip().upper()
        if key not in CHOICE_KEYS or key in by_key: continue
        raw_text = str(item.get("text") or "").strip()
        if len(raw_text) > 50: raise ValueError("每个行动选项正文不得超过 50 字")
        text = clean_text(raw_text, max_chars=50)
        if not text: continue
        risk = _normalize_risk_id(item)
        check = item.get("check"); check = check if isinstance(check, Mapping) else {}
        required = bool(check.get("required", item.get("requires_check", False)))
        stat = clean_text(check.get("attribute_id", item.get("check_stat")), max_chars=40)
        label = clean_text(
            check.get(
                "attribute_label",
                item.get("check_label", stat),
            ),
            max_chars=40,
        ) or stat
        consequence = clean_text(check.get("known_consequences", item.get("known_consequences")), max_chars=300)
        resolution_kind = str(item.get("resolution_kind") or "").strip().lower()
        if not resolution_kind:
            if required:
                resolution_kind = "check"
            elif consequence:
                resolution_kind = "automatic_consequence"
            elif bool(item.get("collective", False)):
                resolution_kind = "vote_only"
            elif risk == "safe":
                resolution_kind = "none"
        if resolution_kind not in {
            "none",
            "check",
            "automatic_consequence",
            "vote_only",
        }:
            raise ValueError("行动选项缺少合法 resolution_kind")
        if risk == "safe" and (
            resolution_kind != "none"
            or required
            or bool(check)
        ):
            raise ValueError("安全选项必须免检且使用 resolution_kind=none")
        if resolution_kind == "check":
            required = True
        elif required:
            raise ValueError("只有 resolution_kind=check 才能要求检定")
        if resolution_kind == "automatic_consequence" and not consequence:
            raise ValueError("自动后果选项必须说明已知代价")
        if resolution_kind == "vote_only" and not bool(item.get("collective", False)):
            raise ValueError("vote_only 选项必须标记 collective=true")
        if risk != "safe" and resolution_kind == "none":
            raise ValueError("非安全选项不能使用 resolution_kind=none")
        risk_label = _RISK_LABELS[risk]
        if contract is not None:
            danger_map = {str(x.get("id")):str(x.get("label")) for x in contract["danger_levels"]}
            if risk not in danger_map: raise ValueError(f"世界包不允许危险度：{risk}")
            risk_label = danger_map[risk]
            mode = contract["resolution"]["mode"]
            if mode in {"none","narrative"}:
                if resolution_kind == "check":
                    raise ValueError("当前世界不启用检定")
                required=False; stat=label=""
            elif required and mode == "attribute":
                matched = attribute_lookup(contract, stat)
                if matched is None:
                    generic=contract["resolution"]["generic_check"]
                    if not generic.get("enabled",False): raise ValueError("需要属性检定时必须使用当前世界声明的属性 ID")
                    stat=""; label=str(generic.get("label") or "通用")
                else:
                    stat,label=matched
                    if stat not in contract["resolution"]["allowed_attributes"]: raise ValueError(f"世界包不允许属性检定：{stat}")
            elif required and mode == "dice_only": stat=label=""
        check_type=str(check.get("type",item.get("check_type","standard")) or "standard").lower()
        if check_type not in {"standard","leader","group","resistance","opposed"}: check_type="standard"
        declared_difficulty = check.get("difficulty", item.get("difficulty"))
        if contract is not None and required:
            mapped_difficulty = contract["resolution"]["difficulty_policy"].get(risk)
            if mapped_difficulty is None:
                raise ValueError(f"危险度 {risk} 不允许配置检定")
            else:
                difficulty = _bounded_int(mapped_difficulty, 12, 5, 25)
        elif required and declared_difficulty in {None, ""}:
            raise ValueError("检定选项缺少难度来源")
        else:
            difficulty=_bounded_int(declared_difficulty,12,5,25)
        adv=check.get("advantage_sources",item.get("advantage_sources")); dis=check.get("disadvantage_sources",item.get("disadvantage_sources"))
        row={"key":key,"text":text,"actor_id":clean_text(item.get("actor_id"),max_chars=128),"risk":risk,"danger_id":risk,"risk_label":risk_label,"resolution_kind":resolution_kind,"requires_check":required,"collective":bool(item.get("collective",False)),"check_type":check_type,"check_stat":stat,"check_label":label,"difficulty":difficulty,"known_consequences":consequence,"advantage_sources":[clean_text(x,max_chars=120) for x in (adv if isinstance(adv,list) else [])[:8] if clean_text(x,max_chars=120)],"disadvantage_sources":[clean_text(x,max_chars=120) for x in (dis if isinstance(dis,list) else [])[:8] if clean_text(x,max_chars=120)]}
        row["check"]={"required":True,"attribute_id":stat,"attribute_label":label,"type":check_type,"difficulty":difficulty,"known_consequences":consequence} if required else None
        by_key[key]=row
    if set(by_key)!=set(CHOICE_KEYS): raise ValueError("每回合必须提供 A、B、C、D 四个有效选项")
    result=[by_key[k] for k in CHOICE_KEYS]
    if not any(x["risk"]=="safe" for x in result): raise ValueError("每组选项至少需要一个安全风险选项")
    # 0.11.3：collective 输出校验——每轮最多 MAX_TEAM_CHOICES 个全队行动，
    # 超出的标记降级为个人选项（防止模型误标导致玩家只能投票、无法行动）。
    collective_indexes = [i for i, item in enumerate(result) if item["collective"]]
    for index in reversed(collective_indexes[MAX_TEAM_CHOICES:]):
        result[index]["collective"] = False
    return result


def normalize_model_choices(value: Any, world: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value,(str,bytes)): raise ValueError("行动选项必须是数组")
    items=[dict(x) for x in value if isinstance(x,Mapping)]
    if len(items)!=len(value): raise ValueError("行动选项必须全部是对象")
    assign=len(items)==4 and not any(str(x.get("key") or "").strip() for x in items)
    for i,item in enumerate(items):
        raw=chr(ord("A")+i) if assign else str(item.get("key") or "")
        match=re.fullmatch(r"(?:选项\s*)?([ABCD])(?:\s*[.、:：)）])?",unicodedata.normalize("NFKC",raw).strip().upper())
        if match: item["key"]=match.group(1)
        if (
            str(item.get("risk") or item.get("danger_id") or "").lower()
            in {"safe", "low"}
            and not str(item.get("resolution_kind") or "").strip()
            and not item.get("check")
            and not item.get("requires_check")
        ):
            item["resolution_kind"] = "none"
    return normalize_choices(items,world)


def opening_choices(world: Mapping[str, Any]) -> list[dict[str, Any]]:
    rules=world.get("rules"); rules=rules if isinstance(rules,Mapping) else {}
    try: return normalize_choices(rules.get("opening_choices"),world)
    except ValueError: return normalize_choices([dict(x) for x in DEFAULT_OPENING_CHOICES],world)


def fallback_choices(state: Mapping[str, Any], world: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    location=clean_text(str(state.get("location") or "当前地点")[:12],max_chars=12); summary=clean_text(str(state.get("scene_summary") or "眼前局势")[:16],max_chars=16)
    check_choice: dict[str, Any] = {
        "key": "C",
        "text": "使用角色已经拥有的能力或物品作一次有限尝试",
        "risk": "controlled",
        "resolution_kind": "check",
        "check": {"required": True, "difficulty": 12},
    }
    if world is not None:
        contract = world_contract(world)
        mode = str(contract["resolution"]["mode"])
        if mode == "attribute":
            allowed = list(contract["resolution"]["allowed_attributes"])
            if allowed:
                check_choice["check"]["attribute_id"] = allowed[0]
        elif mode == "dice_only":
            pass
        else:
            check_choice = {
                "key": "C",
                "text": "接受已知代价，换取一次有限而确定的推进",
                "risk": "controlled",
                "resolution_kind": "automatic_consequence",
                "known_consequences": "局势会留下可见代价，系统将在结算前再次展示。",
            }
    return normalize_choices([
        {"key":"A","text":f"谨慎观察{location}，确认与“{summary}”有关的可见线索","risk":"safe","resolution_kind":"none","requires_check":False},
        {"key":"B","text":"向在场角色询问公开信息，不作强迫或结果预设","risk":"safe","resolution_kind":"none","requires_check":False},
        check_choice,
        {"key":"D","text":"保持警戒并暂缓冒险行动，为下一步搜集更多信息","risk":"safe","resolution_kind":"none","requires_check":False},
    ],world)


def parse_choice_input(value: str) -> tuple[str, str]:
    text = str(value or "").strip()
    match = re.fullmatch(r"([A-Da-d])(?:\s+(.+))?", text, re.S)
    if not match:
        raise ValueError("请选择 A、B、C 或 D，可在字母后补充简短演绎")
    key = match.group(1).upper()
    flavor = clean_text(match.group(2) or "", max_chars=160)
    return key, flavor


_DURATION_PATTERN = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*"
    r"(秒|s|sec|分钟|分|min|m|小时|时|h|天|d)\s*$",
    re.I,
)


def parse_duration(value: str, *, maximum_days: int = 365) -> int:
    match = _DURATION_PATTERN.fullmatch(str(value or ""))
    if not match:
        raise ValueError("时间格式示例：30分钟、2小时、1天")
    amount = float(match.group(1))
    unit = match.group(2).lower()
    multiplier = {
        "秒": 1,
        "s": 1,
        "sec": 1,
        "分钟": 60,
        "分": 60,
        "min": 60,
        "m": 60,
        "小时": 3600,
        "时": 3600,
        "h": 3600,
        "天": 86400,
        "d": 86400,
    }[unit]
    seconds = int(amount * multiplier)
    if seconds <= 0:
        raise ValueError("时间必须大于 0")
    return min(maximum_days * 86400, seconds)

__all__ = [name for name in globals() if not name.startswith('__')]
