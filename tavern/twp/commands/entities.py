from .common import *
from .validation import *
from .scenes import *

def _command_challenge(world: Mapping[str, Any], runtime: dict[str, Any], cmd: dict[str, Any]) -> dict[str, Any]:
    rules = _mapping(world.get("rules"))
    module = _mapping(rules.get("challenge_engine"))
    templates = _index_by(module.get("templates", []))
    action = cmd["action"]
    challenge = runtime.setdefault("challenge", {})
    if not isinstance(challenge, dict):
        challenge = {}
        runtime["challenge"] = challenge
    changes: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    if action == "start":
        template_id = cmd["targets"][0]
        _require_target(template_id, templates, "遭遇模板")
        definition = templates[template_id]
        challenge.update(
            {
                "active_id": template_id,
                "phase": "",
                "status": "active",
                "scene": _text(cmd["payload"].get("scene") or runtime.get("current_scene") or "", 128),
                "participants": _targets(cmd["payload"].get("participants")),
                "started_at": cmd["reason"],
            }
        )
        events.append({"type": "challenge.started", "challenge": template_id})
        summary = f"遭遇开始：{definition.get('label') or template_id}"
    elif action == "advance_phase":
        template_id = str(challenge.get("active_id") or "")
        if not template_id:
            raise WorldCommandError("当前没有进行中的遭遇")
        phases = _sequence(_mapping(templates.get(template_id)).get("phases"))
        current = str(challenge.get("phase") or "")
        if not phases:
            phases = ["opening", "climax", "resolution"]
        try:
            next_index = phases.index(current) + 1 if current in phases else 0
        except ValueError:
            next_index = 0
        if next_index >= len(phases):
            raise WorldCommandError("遭遇已在最终阶段")
        challenge["phase"] = phases[next_index]
        events.append({"type": "challenge.phase_advanced", "challenge": template_id, "phase": phases[next_index]})
        summary = f"遭遇阶段推进：{phases[next_index]}"
    elif action in {"end", "victory", "defeat", "retreat"}:
        template_id = str(challenge.get("active_id") or "")
        if not template_id:
            raise WorldCommandError("当前没有进行中的遭遇")
        outcome = (
            "victory" if action == "victory"
            else "defeat" if action == "defeat"
            else "retreat" if action == "retreat"
            else _text(cmd["payload"].get("outcome") or "ended", 40)
        )
        challenge["status"] = "ended"
        challenge["outcome"] = outcome
        challenge["ended_at"] = cmd["reason"]
        events.append({"type": "challenge.ended", "challenge": template_id, "outcome": outcome})
        summary = f"遭遇结束：{outcome}"
    else:
        raise WorldCommandError(f"不支持的 challenge 命令：{action}")
    runtime["challenge"] = challenge
    changes.append({"domain": "challenge", "action": action, "target": str(challenge.get("active_id") or "")})
    return {"changes": changes, "events": events, "summary": summary}


def _command_progression(world: Mapping[str, Any], runtime: dict[str, Any], cmd: dict[str, Any]) -> dict[str, Any]:
    rules = _mapping(world.get("rules"))
    module = _mapping(rules.get("progression"))
    tracks = _index_by(module.get("tracks", []))
    action = cmd["action"]
    target = cmd["targets"][0]
    _require_target(target, tracks, "成长轨迹")
    progression = runtime.setdefault("progression", {})
    if not isinstance(progression, dict):
        progression = {}
        runtime["progression"] = progression
    track_values = progression.setdefault("tracks", {})
    if not isinstance(track_values, dict):
        track_values = {}
    milestones = progression.setdefault("milestones", [])
    if not isinstance(milestones, list):
        milestones = []
    definition = tracks[target]
    changes: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    if action == "advance":
        try:
            amount = int(cmd["payload"].get("amount", 1) or 1)
        except (TypeError, ValueError):
            raise WorldCommandError("progression.advance 需要整数 amount")
        track_values[target] = int(track_values.get(target, 0) or 0) + max(1, min(10, amount))
        events.append({"type": "progression.advanced", "track": target, "amount": amount})
        summary = f"成长轨迹「{definition.get('label') or target}」+{amount}"
    elif action == "unlock_milestone":
        milestone = _text(cmd["payload"].get("milestone") or (cmd["targets"][1] if len(cmd["targets"]) > 1 else ""), 80)
        if not milestone:
            raise WorldCommandError("progression.unlock_milestone 需要 payload.milestone")
        entry = {"track": target, "milestone": milestone}
        if entry not in milestones:
            milestones.append(entry)
        events.append({"type": "progression.milestone_unlocked", "track": target, "milestone": milestone})
        summary = f"里程碑解锁：{definition.get('label') or target} / {milestone}"
    else:
        raise WorldCommandError(f"不支持的 progression 命令：{action}")
    progression["tracks"] = track_values
    progression["milestones"] = milestones
    runtime["progression"] = progression
    changes.append({"domain": "progression", "action": action, "target": target})
    return {"changes": changes, "events": events, "summary": summary}


def _crafting_inventory(runtime: dict[str, Any]) -> dict[str, Any]:
    inventory = runtime.setdefault("items_inventory", {})
    if not isinstance(inventory, dict):
        raise WorldCommandError("物品运行态损坏，无法安全执行制作")
    owners = inventory.setdefault("owners", {})
    if not isinstance(owners, dict):
        raise WorldCommandError("物品所有者运行态损坏，无法安全执行制作")
    return owners


def _item_quantity(owner_inventory: Mapping[str, Any], item_id: str) -> int:
    value = owner_inventory.get(item_id, 0)
    if isinstance(value, Mapping):
        value = value.get("quantity", 0)
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        raise WorldCommandError(f"物品数量损坏：{item_id}")


def _set_item_quantity(owner_inventory: dict[str, Any], item_id: str, quantity: int) -> None:
    before = owner_inventory.get(item_id)
    quantity = max(0, int(quantity))
    if isinstance(before, Mapping):
        value = dict(before)
        value["quantity"] = quantity
        owner_inventory[item_id] = value
    elif quantity:
        owner_inventory[item_id] = quantity
    else:
        owner_inventory.pop(item_id, None)


def _material_plan(value: Any, *, label: str) -> dict[str, int]:
    plan: dict[str, int] = {}
    for item in _sequence(value):
        raw = _mapping(item)
        item_id = _text(raw.get("item") or raw.get("item_id") or raw.get("id"), 160)
        try:
            quantity = int(raw.get("quantity") or 0)
        except (TypeError, ValueError):
            quantity = 0
        if not item_id or quantity <= 0:
            raise WorldCommandError(f"{label}必须声明有效物品与正整数数量")
        plan[item_id] = plan.get(item_id, 0) + quantity
    return plan


def _reserved_materials(projects: list[Any], owner: str, *, excluding: str = "") -> dict[str, int]:
    reserved: dict[str, int] = {}
    for value in projects:
        project = _mapping(value)
        if (
            str(project.get("owner") or "") != owner
            or str(project.get("status") or "") != "in_progress"
            or str(project.get("id") or "") == excluding
        ):
            continue
        for item_id, quantity in _mapping(project.get("materials_locked")).items():
            reserved[str(item_id)] = reserved.get(str(item_id), 0) + max(0, int(quantity or 0))
    return reserved


def _consume_plan(owner_inventory: dict[str, Any], plan: Mapping[str, Any]) -> dict[str, int]:
    consumed: dict[str, int] = {}
    for item_id, raw_quantity in plan.items():
        quantity = max(0, int(raw_quantity or 0))
        before = _item_quantity(owner_inventory, str(item_id))
        if before < quantity:
            raise WorldCommandError(f"制作材料不足：{item_id} 需要 {quantity}，当前 {before}")
        _set_item_quantity(owner_inventory, str(item_id), before - quantity)
        if quantity:
            consumed[str(item_id)] = quantity
    return consumed


def _failure_consumption(recipe: Mapping[str, Any], inputs: Mapping[str, int]) -> dict[str, int]:
    policy = _mapping(recipe.get("failure")).get("consume", "none")
    if isinstance(policy, Mapping):
        return {
            str(item_id): min(max(0, int(quantity or 0)), int(inputs.get(str(item_id), 0)))
            for item_id, quantity in policy.items()
            if str(item_id) in inputs and int(quantity or 0) > 0
        }
    if policy == "all":
        return dict(inputs)
    if policy == "partial":
        ordered = sorted(inputs.items())
        return dict(ordered[: max(1, (len(ordered) + 1) // 2)])
    return {}


def _command_crafting(world: Mapping[str, Any], runtime: dict[str, Any], cmd: dict[str, Any]) -> dict[str, Any]:
    rules = _mapping(world.get("rules"))
    module = _mapping(rules.get("crafting"))
    recipes = _index_by(module.get("recipes", []))
    action = cmd["action"]
    crafting = runtime.setdefault("crafting", {})
    if not isinstance(crafting, dict):
        crafting = {}
        runtime["crafting"] = crafting
    projects = crafting.setdefault("projects", [])
    if not isinstance(projects, list):
        projects = []
    owners = _crafting_inventory(runtime)
    changes: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    if action == "start":
        recipe_id = cmd["targets"][0]
        _require_target(recipe_id, recipes, "配方")
        owner = _text(cmd["payload"].get("owner") or cmd["operator"], 128)
        if not owner:
            raise WorldCommandError("crafting.start 需要 payload.owner")
        project_id = _text(cmd["payload"].get("project_id") or f"project:{recipe_id}:{owner}", 160)
        existing = next((_mapping(item) for item in projects if str(_mapping(item).get("id")) == project_id), {})
        if existing:
            if str(existing.get("recipe_id")) != recipe_id or str(existing.get("owner")) != owner:
                raise WorldCommandError(f"制作项目标识已用于另一份计划：{project_id}")
            events.append({"type": "crafting.start_replayed", "project": project_id, "recipe": recipe_id})
            summary = f"制作项目已存在，返回原计划：{_mapping(recipes.get(recipe_id)).get('label') or recipe_id}"
            crafting["projects"] = projects
            runtime["crafting"] = crafting
            changes.append({"domain": "crafting", "action": "start_replayed", "target": project_id})
            return {"changes": changes, "events": events, "summary": summary}
        recipe = _mapping(recipes.get(recipe_id))
        inputs = _material_plan(recipe.get("inputs"), label="配方输入")
        outputs = _material_plan(recipe.get("outputs"), label="配方产物")
        if not inputs or not outputs:
            raise WorldCommandError("配方必须声明至少一种输入和产物")
        owner_inventory = owners.setdefault(owner, {})
        if not isinstance(owner_inventory, dict):
            raise WorldCommandError("制作所有者的背包运行态损坏")
        reserved = _reserved_materials(projects, owner)
        for item_id, quantity in inputs.items():
            available = _item_quantity(owner_inventory, item_id) - reserved.get(item_id, 0)
            if available < quantity:
                raise WorldCommandError(f"制作材料不足或已被其他项目锁定：{item_id} 需要 {quantity}，可用 {max(0, available)}")
        project = {
            "id": project_id,
            "recipe_id": recipe_id,
            "owner": owner,
            "collaborators": _targets(cmd["payload"].get("collaborators")),
            "materials_locked": inputs,
            "outputs_locked": outputs,
            "consumption_policy": str(recipe.get("material_consumption") or "on_resolve"),
            "failure_policy": dict(_mapping(recipe.get("failure"))),
            "status": "in_progress",
            "started_at": cmd["reason"],
        }
        projects.append(project)
        events.append({"type": "crafting.started", "project": project_id, "recipe": recipe_id})
        summary = f"制作项目开始：{_mapping(recipes.get(recipe_id)).get('label') or recipe_id}"
    elif action == "resolve":
        project_id = cmd["targets"][0]
        project = next((item for item in projects if str(item.get("id")) == project_id), None)
        if not project:
            raise WorldCommandError(f"制作项目不存在：{project_id}")
        outcome = _text(cmd["payload"].get("outcome") or "success", 40)
        if outcome not in {"success", "failure"}:
            raise WorldCommandError("制作结果只允许 success 或 failure")
        if str(project.get("status") or "") != "in_progress":
            if str(project.get("outcome") or "") == outcome:
                events.append({"type": "crafting.resolve_replayed", "project": project_id, "outcome": outcome})
                summary = f"制作结算已存在：{project_id} → {outcome}"
                crafting["projects"] = projects
                runtime["crafting"] = crafting
                changes.append({"domain": "crafting", "action": "resolve_replayed", "target": project_id})
                return {"changes": changes, "events": events, "summary": summary}
            raise WorldCommandError("制作项目已经结算，不能改写结果")
        owner = str(project.get("owner") or "")
        owner_inventory = owners.setdefault(owner, {})
        if not isinstance(owner_inventory, dict):
            raise WorldCommandError("制作所有者的背包运行态损坏")
        inputs = {str(key): max(0, int(value or 0)) for key, value in _mapping(project.get("materials_locked")).items()}
        outputs = {str(key): max(0, int(value or 0)) for key, value in _mapping(project.get("outputs_locked")).items()}
        recipe = _mapping(recipes.get(str(project.get("recipe_id") or "")))
        consume = inputs if outcome == "success" else _failure_consumption(recipe, inputs)
        consumed = _consume_plan(owner_inventory, consume)
        produced: dict[str, int] = {}
        if outcome == "success":
            for item_id, quantity in outputs.items():
                before = _item_quantity(owner_inventory, item_id)
                _set_item_quantity(owner_inventory, item_id, before + quantity)
                produced[item_id] = quantity
        project["status"] = "completed" if outcome == "success" else "failed"
        project["outcome"] = outcome
        project["quality"] = _text(cmd["payload"].get("quality"), 40)
        project["consumed"] = consumed
        project["produced"] = produced
        project["resolved_at"] = cmd["reason"]
        events.append({"type": "crafting.resolved", "project": project_id, "outcome": outcome, "consumed": consumed, "produced": produced})
        summary = f"制作结算：{project_id} → {outcome}"
    elif action == "abort":
        project_id = cmd["targets"][0]
        project = next((item for item in projects if str(item.get("id")) == project_id), None)
        if not project:
            raise WorldCommandError(f"制作项目不存在：{project_id}")
        if str(project.get("status") or "") == "aborted":
            events.append({"type": "crafting.abort_replayed", "project": project_id})
            summary = f"制作中止回执已存在：{project_id}"
            crafting["projects"] = projects
            runtime["crafting"] = crafting
            changes.append({"domain": "crafting", "action": "abort_replayed", "target": project_id})
            return {"changes": changes, "events": events, "summary": summary}
        if str(project.get("status") or "") != "in_progress":
            raise WorldCommandError("已经结算的制作项目不能中止")
        project["status"] = "aborted"
        project["aborted_at"] = cmd["reason"]
        events.append({"type": "crafting.aborted", "project": project_id})
        summary = f"制作中止：{project_id}"
    else:
        raise WorldCommandError(f"不支持的 crafting 命令：{action}")
    crafting["projects"] = projects
    runtime["crafting"] = crafting
    changes.append({"domain": "crafting", "action": action, "target": cmd["targets"][0]})
    return {"changes": changes, "events": events, "summary": summary}


def _command_handout(world: Mapping[str, Any], runtime: dict[str, Any], cmd: dict[str, Any]) -> dict[str, Any]:
    rules = _mapping(world.get("rules"))
    module = _mapping(rules.get("maps_handouts"))
    # B2：手册命令同时覆盖 handouts 与 maps（地图也支持解锁/发送）。
    handouts = _index_by(module.get("handouts", []))
    for _map_item in _sequence(module.get("maps")):
        if isinstance(_map_item, Mapping) and _map_item.get("id"):
            handouts.setdefault(str(_map_item["id"]), dict(_map_item))
    action = cmd["action"]
    target = cmd["targets"][0]
    _require_target(target, handouts, "手册/地图")
    state = runtime.setdefault("handouts", {})
    if not isinstance(state, dict):
        state = {}
        runtime["handouts"] = state
    unlocked = state.setdefault("unlocked", [])
    if not isinstance(unlocked, list):
        unlocked = []
    sent = state.setdefault("sent", [])
    if not isinstance(sent, list):
        sent = []
    definition = handouts[target]
    changes: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    if action == "unlock":
        if target not in unlocked:
            unlocked.append(target)
        events.append({"type": "handout.unlocked", "handout": target, "visibility": cmd["visibility"]})
        summary = f"手册解锁：{definition.get('label') or target}"
    elif action in {"send", "resend"}:
        if target not in unlocked:
            raise WorldCommandError(f"手册尚未解锁：{target}")
        entry = {"handout": target, "recipients": _targets(cmd["payload"].get("recipients")), "at": cmd["reason"]}
        sent.append(entry)
        events.append({"type": "handout.sent", "handout": target})
        summary = f"手册已发送：{definition.get('label') or target}"
    else:
        raise WorldCommandError(f"不支持的 handout 命令：{action}")
    state["unlocked"] = unlocked
    state["sent"] = sent[-100:]
    runtime["handouts"] = state
    changes.append({"domain": "handout", "action": action, "target": target})
    return {"changes": changes, "events": events, "summary": summary}


__all__ = [name for name in globals() if not name.startswith('__')]

