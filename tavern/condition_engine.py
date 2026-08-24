from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .entity_registry import EntityRegistry, module_value, split_ref


SYMBOL_OPERATORS = frozenset({"==", "!=", ">", ">=", "<", "<="})
WORD_OPERATORS = frozenset({"eq", "ne", "gt", "gte", "lt", "lte"})
EXTENDED_OPERATORS = frozenset(
    {
        "in", "not_in", "contains", "not_contains", "intersects",
        "exists", "not_exists", "matches_ref",
    }
)
SPECIAL_OPERATORS = frozenset({"count", "state_is", "capability_declared"})
OPERATORS = frozenset(
    SYMBOL_OPERATORS | WORD_OPERATORS | EXTENDED_OPERATORS | SPECIAL_OPERATORS
)
LOGICAL_OPERATORS = frozenset({"all", "any", "none", "not"})
# Leaf nodes may declare the operator as a bare key, e.g. {"path": ..., "eq": true}.
COMPARISON_KEYWORDS = frozenset(
    WORD_OPERATORS | SYMBOL_OPERATORS | EXTENDED_OPERATORS | {"count"}
)
_OPERATOR_KEY_ORDER = (
    "eq", "ne", "gt", "gte", "lt", "lte",
    "==", "!=", ">", ">=", "<", "<=",
    "in", "not_in", "contains", "not_contains", "intersects",
    "exists", "not_exists", "matches_ref", "count",
)
_WORD_TO_SYMBOL = {
    "eq": "==",
    "ne": "!=",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
}
SCOPES = frozenset(
    {
        "actor", "target", "action", "scene", "world", "session", "party",
        "location", "object", "relationship", "event", "runtime_effect", "custom",
    }
)

_DECLARABLE_FIELDS = frozenset(
    {"code", "message", "recovery", "technical_refs"}
)


@dataclass
class ConditionResult:
    matched: bool
    reads: list[dict[str, Any]]


@dataclass
class ConditionEvaluation:
    """D1-RUN-004 condition result: a boolean alone is not enough."""

    matched: bool
    reads: list[dict[str, Any]]
    code: str = ""
    message: str = ""
    recovery: str = ""
    technical_refs: list[str] = field(default_factory=list)
    failed_node: dict[str, Any] | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "allowed": bool(self.matched),
            "code": str(self.code or ("condition.matched" if self.matched else "condition.not_matched")),
            "message": str(self.message or ""),
            "recovery": str(self.recovery or ""),
            "technical_refs": [str(item) for item in self.technical_refs],
        }


def _sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes))


class ConditionEngine:
    MAX_DEPTH = 12
    MAX_NODES = 256

    def __init__(self, registry: EntityRegistry) -> None:
        self.registry = registry

    @staticmethod
    def _read_path(context: Mapping[str, Any], path: str) -> tuple[Any, bool]:
        current: Any = context
        for segment in (item for item in str(path or "").split(".") if item):
            if isinstance(current, Mapping) and segment in current:
                current = current[segment]
            else:
                return None, False
        return current, True

    def _read_operand(
        self,
        operand: Any,
        context: Mapping[str, Any],
        reads: list[dict[str, Any]],
        *,
        default_scope: str = "actor",
    ) -> Any:
        """Resolve one operand (literal / scope+ref / dotted path)."""
        if isinstance(operand, Mapping):
            if "path" in operand:
                path = str(operand.get("path") or "")
                value, found = self._read_path(context, path)
                reads.append({"path": path, "found": found, "value": value})
                return value
            if "value" in operand and "scope" not in operand:
                return operand.get("value")
            scope = str(operand.get("scope") or default_scope).strip()
            ref = str(operand.get("ref") or "").strip()
            return self._read_scope_ref(
                scope,
                ref,
                context,
                reads,
                allow_runtime_ref=bool(operand.get("allow_runtime_ref", False)),
            )
        if isinstance(operand, str) and operand.startswith("path:"):
            path = operand[len("path:"):].strip()
            value, found = self._read_path(context, path)
            reads.append({"path": path, "found": found, "value": value})
            return value
        return operand

    def _read_scope_ref(
        self,
        scope: str,
        ref: str,
        context: Mapping[str, Any],
        reads: list[dict[str, Any]],
        *,
        allow_runtime_ref: bool = False,
    ) -> Any:
        if scope not in SCOPES:
            raise ValueError(f"条件读取了未注册作用域：{scope or '<empty>'}")
        source = context.get(scope, {})
        runtime_ref_present = False
        if isinstance(source, Mapping) and ref:
            refs = source.get("refs")
            runtime_ref_present = (
                isinstance(refs, Mapping) and ref in refs
            ) or ref in source
        if ref:
            split_ref(ref)
            if (
                not self.registry.contains(ref)
                and not allow_runtime_ref
                and not runtime_ref_present
            ):
                raise ValueError(f"条件引用未注册：{ref}")
        value = None
        found = False
        if isinstance(source, Mapping):
            refs = source.get("refs")
            if ref and isinstance(refs, Mapping) and ref in refs:
                value, found = refs[ref], True
            elif ref and ref in source:
                value, found = source[ref], True
            elif not ref:
                value, found = source, True
        reads.append({"scope": scope, "ref": ref, "found": found, "value": value})
        return value

    def _read(self, operand: Any, context: Mapping[str, Any], reads: list[dict[str, Any]]) -> Any:
        """Backward-compatible entry used by existing callers."""
        if not isinstance(operand, Mapping):
            return operand
        if "path" in operand:
            path = str(operand.get("path") or "")
            value, found = self._read_path(context, path)
            reads.append({"path": path, "found": found, "value": value})
            return value
        if "value" in operand and "scope" not in operand:
            return operand.get("value")
        return self._read_scope_ref(
            str(operand.get("scope") or "actor"),
            str(operand.get("ref") or ""),
            context,
            reads,
            allow_runtime_ref=bool(operand.get("allow_runtime_ref", False)),
        )

    @staticmethod
    def _compare(left: Any, operator: str, right: Any) -> bool:
        normalized = _WORD_TO_SYMBOL.get(str(operator or "").lower(), str(operator or ""))
        if normalized not in OPERATORS:
            raise ValueError(f"不支持的条件运算符：{operator}")
        operator = normalized
        if operator == "exists":
            return left is not None
        if operator == "not_exists":
            return left is None
        if operator == "==" or operator == "matches_ref":
            return left == right
        if operator == "!=":
            return left != right
        if operator in {">", ">=", "<", "<="}:
            if isinstance(left, bool) or isinstance(right, bool):
                raise TypeError("布尔值不能参与数值大小比较")
            try:
                a, b = float(left), float(right)
            except (TypeError, ValueError, OverflowError) as exc:
                raise TypeError("大小比较的双方必须是数值") from exc
            return {">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b}[operator]
        if operator in {"in", "not_in"}:
            result = left in right if _sequence(right) or isinstance(right, (set, Mapping)) else False
            return result if operator == "in" else not result
        if operator in {"contains", "not_contains"}:
            result = right in left if _sequence(left) or isinstance(left, (str, set, Mapping)) else False
            return result if operator == "contains" else not result
        if operator == "intersects":
            return bool(set(left or []) & set(right or []))
        return False

    def _count_length(self, operand: Any, context: Mapping[str, Any], reads: list[dict[str, Any]]) -> int:
        value = self._read_operand(operand, context, reads)
        if value is None:
            return 0
        if isinstance(value, Mapping):
            return len(value)
        if _sequence(value) or isinstance(value, (set, frozenset)):
            return len(value)
        if isinstance(value, (str, bytes)):
            return 0
        return 0

    def _state_of(self, value: Any) -> Any:
        if isinstance(value, Mapping):
            for key in ("fate_state", "state", "status"):
                if key in value:
                    return value[key]
            return None
        return value

    def _capability_declared(self, spec: Any) -> bool:
        world = self.registry.world
        if isinstance(spec, Mapping):
            capability = str(spec.get("capability") or spec.get("id") or "")
            requested_version = str(spec.get("version") or "").strip() or None
        else:
            text = str(spec or "").strip()
            capability, separator, version_text = text.partition("@")
            capability = capability.strip()
            requested_version = version_text.strip() or None
        if not capability:
            raise ValueError("capability_declared 需要能力标识")
        declared_versions: list[str] = []
        features = module_value(world, "protocol", {})
        if isinstance(features, Mapping):
            feature_map = features.get("features")
            if isinstance(feature_map, Mapping):
                for key, value in feature_map.items():
                    if str(key) == capability and value not in (None, ""):
                        declared_versions.append(str(value))
        required = world.get("required_features")
        if isinstance(required, Sequence) and not isinstance(required, (str, bytes)):
            for entry in required:
                text = str(entry or "").strip()
                base, _, constraint = text.partition("@")
                if base == capability:
                    declared_versions.append(constraint.strip())
        if not declared_versions:
            return False
        if requested_version is None:
            return True
        return any(
            _version_satisfied(entry, requested_version)
            for entry in declared_versions
        )

    def _evaluate_node(
        self,
        node: Any,
        context: Mapping[str, Any],
        reads: list[dict[str, Any]],
        state: dict[str, Any],
        depth: int,
        inherited: dict[str, Any] | None,
    ) -> bool:
        state["nodes"] += 1
        if state["nodes"] > self.MAX_NODES:
            raise ValueError("条件节点数量超过技术安全上限")
        if depth > self.MAX_DEPTH:
            raise ValueError("条件嵌套超过技术安全上限")
        if node is None or node == {}:
            return True
        if isinstance(node, bool):
            return node
        if not isinstance(node, Mapping):
            raise TypeError("条件必须是对象")
        declared = {
            key: node[key]
            for key in _DECLARABLE_FIELDS
            if key in node
        }
        fallback = {**(inherited or {}), **declared}

        def record_failure() -> bool:
            if state["first_failure"] is None:
                state["first_failure"] = {"node": dict(node), "fallback": dict(fallback)}
            return False

        if "all" in node:
            values = node["all"]
            if not _sequence(values):
                raise TypeError("all 必须是数组")
            for item in values:
                if not self._evaluate_node(item, context, reads, state, depth + 1, fallback):
                    return record_failure()
            return True
        if "any" in node:
            values = node["any"]
            if not _sequence(values):
                raise TypeError("any 必须是数组")
            for item in values:
                if self._evaluate_node(item, context, reads, state, depth + 1, fallback):
                    return True
            return record_failure()
        if "none" in node:
            values = node["none"]
            if not _sequence(values):
                raise TypeError("none 必须是数组")
            for item in values:
                if self._evaluate_node(item, context, reads, state, depth + 1, fallback):
                    return record_failure()
            return True
        if "not" in node:
            matched = self._evaluate_node(node["not"], context, reads, state, depth + 1, fallback)
            if matched:
                return record_failure()
            return True
        if "capability_declared" in node:
            matched = self._capability_declared(node["capability_declared"])
            reads.append(
                {
                    "operator": "capability_declared",
                    "capability": node["capability_declared"],
                    "found": matched,
                }
            )
            return matched or record_failure()
        if "state_is" in node:
            spec = node.get("state_is")
            target_state = node.get("state", node.get("value"))
            if isinstance(spec, Mapping):
                value = self._read_operand(spec, context, reads)
            elif isinstance(spec, str) and ":" in spec:
                value = self._read_operand(
                    {"scope": node.get("scope", "actor"), "ref": spec},
                    context,
                    reads,
                )
            else:
                value = self._read_operand(
                    {"scope": node.get("scope", "actor"), "ref": str(spec or "")},
                    context,
                    reads,
                )
            matched = self._state_of(value) == target_state
            return matched or record_failure()
        if "count" in node and not isinstance(node.get("count"), (int, float)):
            count = self._count_length(node["count"], context, reads)
            operator_key = next(
                (
                    key for key in (
                        "eq", "ne", "gt", "gte", "lt", "lte",
                        "==", "!=", ">", ">=", "<", "<=",
                    )
                    if key in node
                ),
                "",
            )
            if not operator_key:
                raise ValueError("count 比较缺少数值运算符")
            matched = self._compare(
                count,
                operator_key,
                node.get(operator_key),
            )
            return matched or record_failure()

        compare = node.get("compare") if isinstance(node.get("compare"), Mapping) else node
        operator = str(compare.get("operator") or "")
        if operator:
            operator = _WORD_TO_SYMBOL.get(operator.lower(), operator)
            left_spec = compare.get("left")
            if left_spec is None and "ref" in compare:
                left_spec = {
                    "scope": compare.get("scope", "actor"),
                    "ref": compare.get("ref"),
                    "allow_runtime_ref": bool(
                        compare.get("allow_runtime_ref", False)
                    ),
                }
            right_spec = compare.get("right", compare.get("value"))
            left = self._read_operand(left_spec, context, reads)
            right = self._read_operand(right_spec, context, reads)
        else:
            operator_key = next(
                (key for key in _OPERATOR_KEY_ORDER if key in compare),
                "",
            )
            if not operator_key:
                raise ValueError("条件叶节点缺少运算符")
            if operator_key == "count":
                count_operand = (
                    {"path": compare.get("path")}
                    if "path" in compare
                    else compare.get("left")
                )
                count = self._count_length(count_operand, context, reads)
                matched = self._compare(count, "==", compare.get("count"))
                return matched or record_failure()
            operator = _WORD_TO_SYMBOL.get(operator_key, operator_key)
            if "path" in compare:
                path = str(compare.get("path") or "")
                left, found = self._read_path(context, path)
                reads.append({"path": path, "found": found, "value": left})
            else:
                left_spec = compare.get("left")
                if left_spec is None and "ref" in compare:
                    left_spec = {
                        "scope": compare.get("scope", "actor"),
                        "ref": compare.get("ref"),
                        "allow_runtime_ref": bool(
                            compare.get("allow_runtime_ref", False)
                        ),
                    }
                left = self._read_operand(left_spec, context, reads)
            right = compare.get(operator_key, compare.get("value"))
        offset = compare.get("offset")
        if offset not in (None, 0) and right is not None:
            try:
                right = float(right) + float(offset)
            except (TypeError, ValueError, OverflowError) as exc:
                raise TypeError("offset 只能用于数值比较") from exc
        matched = self._compare(left, operator, right)
        return matched or record_failure()

    def evaluate(self, condition: Any, context: Mapping[str, Any]) -> ConditionResult:
        reads: list[dict[str, Any]] = []
        state: dict[str, Any] = {"nodes": 0, "first_failure": None}
        matched = self._evaluate_node(condition, context, reads, state, 0, None)
        return ConditionResult(matched=matched, reads=reads)

    def evaluate_with_detail(
        self,
        condition: Any,
        context: Mapping[str, Any],
    ) -> ConditionEvaluation:
        """D1-RUN-004: return allowed + stable code/message/recovery."""

        reads: list[dict[str, Any]] = []
        state: dict[str, Any] = {"nodes": 0, "first_failure": None}
        matched = self._evaluate_node(condition, context, reads, state, 0, None)
        if matched:
            return ConditionEvaluation(
                matched=True,
                reads=reads,
                code="condition.matched",
            )
        failure = state["first_failure"] or {}
        node = failure.get("node") or {}
        fallback = failure.get("fallback") or {}
        technical = fallback.get("technical_refs") or []
        if not isinstance(technical, Sequence) or isinstance(technical, (str, bytes)):
            technical = []
        return ConditionEvaluation(
            matched=False,
            reads=reads,
            code=str(fallback.get("code") or "condition.not_matched"),
            message=str(fallback.get("message") or ""),
            recovery=str(fallback.get("recovery") or ""),
            technical_refs=[str(item) for item in technical],
            failed_node=dict(node),
        )


def _version_satisfied(entry_version: str, requested: str) -> bool:
    """Minimal declarative version check: exact, or >=/=/<= constraint prefixes."""

    entry = str(entry_version or "").strip()
    entry_operator = ""
    for prefix in (">=", "<=", ">", "<", "=", "=="):
        if entry.startswith(prefix):
            entry_operator = prefix
            entry = entry[len(prefix):].strip()
            break
    requested_clean = str(requested or "").strip().lstrip("= ")

    def parts(value: str) -> tuple[int, ...]:
        chunks: list[int] = []
        for item in value.split("."):
            digits = "".join(character for character in item if character.isdigit())
            chunks.append(int(digits) if digits else 0)
        return tuple(chunks)

    try:
        left, right = parts(entry), parts(requested_clean)
    except (TypeError, ValueError):
        return entry == requested_clean
    if entry_operator in {"", "=", "=="}:
        return left == right
    if entry_operator == ">=":
        return left >= right
    if entry_operator == "<=":
        return left <= right
    if entry_operator == ">":
        return left > right
    if entry_operator == "<":
        return left < right
    return left == right


def validate_condition_tree(
    condition: Any,
    *,
    registry: EntityRegistry | None = None,
) -> list[str]:
    """Declarative tree validator shared by command/terminal/world gates."""

    problems: list[str] = []
    nodes = 0

    def walk(node: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > ConditionEngine.MAX_NODES:
            problems.append("条件节点数量超过技术安全上限")
            return
        if depth > ConditionEngine.MAX_DEPTH:
            problems.append("条件嵌套超过技术安全上限")
            return
        if node is None or node == {} or isinstance(node, bool):
            return
        if not isinstance(node, Mapping):
            problems.append("条件必须是对象")
            return
        if "all" in node or "any" in node or "none" in node:
            key = "all" if "all" in node else ("any" if "any" in node else "none")
            values = node[key]
            if not _sequence(values):
                problems.append(f"{key} 必须是数组")
                return
            for item in values:
                walk(item, depth + 1)
            return
        if "not" in node:
            walk(node["not"], depth + 1)
            return
        if "capability_declared" in node:
            spec = node["capability_declared"]
            capability = (
                str(spec.get("capability") or spec.get("id") or "")
                if isinstance(spec, Mapping)
                else str(spec or "").partition("@")[0].strip()
            )
            if not capability:
                problems.append("capability_declared 缺少能力标识")
            return
        if "state_is" in node:
            spec = node["state_is"]
            if isinstance(spec, Mapping):
                ref = str(spec.get("ref") or "")
                if ref and registry is not None and not registry.contains(ref):
                    problems.append(f"state_is 引用未注册：{ref}")
            elif isinstance(spec, str) and ":" in spec:
                try:
                    split_ref(spec)
                    if registry is not None and not registry.contains(spec):
                        problems.append(f"state_is 引用未注册：{spec}")
                except ValueError as exc:
                    problems.append(str(exc))
            return
        compare = node.get("compare") if isinstance(node.get("compare"), Mapping) else node
        operator = str(compare.get("operator") or "")
        if operator:
            operator = _WORD_TO_SYMBOL.get(operator.lower(), operator)
            if operator not in OPERATORS:
                problems.append(f"不支持的条件运算符：{operator}")
        else:
            operator_key = next(
                (key for key in _OPERATOR_KEY_ORDER if key in compare),
                "",
            )
            if not operator_key:
                problems.append("条件叶节点缺少运算符")
            elif operator_key == "count":
                if isinstance(compare.get("count"), (int, float)):
                    # 紧凑形式 {"path": ..., "count": N}，含义为数量 == N。
                    pass
                else:
                    comparator = next(
                        (
                            key for key in (
                                "eq", "ne", "gt", "gte", "lt", "lte",
                                "==", "!=", ">", ">=", "<", "<=",
                            )
                            if key in compare
                        ),
                        "",
                    )
                    if not comparator:
                        problems.append("count 比较缺少数值运算符")
        for operand_key in ("left", "right", "ref"):
            if operand_key not in compare:
                continue
            operand = compare[operand_key]
            if not isinstance(operand, Mapping):
                continue
            if "path" in operand or "value" in operand:
                continue
            ref = str(operand.get("ref") or "")
            if ref:
                try:
                    split_ref(ref)
                    if (
                        registry is not None
                        and not registry.contains(ref)
                        and not bool(operand.get("allow_runtime_ref", False))
                    ):
                        problems.append(f"条件引用未注册：{ref}")
                except ValueError as exc:
                    problems.append(str(exc))
        if "path" in compare:
            path = str(compare.get("path") or "")
            if not path or path.startswith(".") or path.endswith("."):
                problems.append("条件 path 必须是非空点路径")

    walk(condition, 0)
    return problems


def assert_valid_condition_tree(
    condition: Any,
    *,
    registry: EntityRegistry | None = None,
) -> None:
    problems = validate_condition_tree(condition, registry=registry)
    if problems:
        raise ValueError("条件树校验失败：" + "；".join(problems))


__all__ = [
    "COMPARISON_KEYWORDS",
    "ConditionEngine",
    "ConditionEvaluation",
    "ConditionResult",
    "EXTENDED_OPERATORS",
    "LOGICAL_OPERATORS",
    "OPERATORS",
    "SCOPES",
    "SPECIAL_OPERATORS",
    "SYMBOL_OPERATORS",
    "WORD_OPERATORS",
    "assert_valid_condition_tree",
    "validate_condition_tree",
]
