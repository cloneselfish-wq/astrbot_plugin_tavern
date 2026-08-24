"""request query normalization.

AstrBot adapters expose query parameters through several incompatible proxy
shapes.  This module is deliberately host-independent and converts those
shapes into one immutable, versioned view before route code reads values.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote_plus

from .errors import BadRequestError

QUERY_SCHEMA = "tavern-normalized-query/1.0.0-rc10"


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise BadRequestError(
                "查询参数不是有效的 UTF-8。",
                code="tavern.query.invalid_encoding",
                recovery="请重新输入该参数后再试。",
                detail=str(exc),
            ) from exc
    text = str(value)
    try:
        return unquote_plus(text, encoding="utf-8", errors="strict")
    except (UnicodeDecodeError, ValueError) as exc:
        raise BadRequestError(
            "查询参数的 URL 编码无效。",
            code="tavern.query.invalid_encoding",
            recovery="请重新输入该参数后再试。",
            detail=str(exc),
        ) from exc


def _iter_pairs(query: Any) -> list[tuple[str, Any]] | None:
    if isinstance(query, Mapping):
        pairs: list[tuple[str, Any]] = []
        for key, value in query.items():
            if isinstance(value, (list, tuple)):
                pairs.extend((str(key), item) for item in value)
            else:
                pairs.append((str(key), value))
        return pairs
    items = getattr(query, "items", None)
    if callable(items):
        try:
            return [(str(key), value) for key, value in items()]
        except (TypeError, ValueError):
            pass
    if isinstance(query, Iterable) and not isinstance(query, (str, bytes)):
        try:
            return [(str(key), value) for key, value in query]
        except (TypeError, ValueError):
            return None
    return None


@dataclass(frozen=True, slots=True)
class NormalizedQuery:
    single: Mapping[str, str]
    multi: Mapping[str, tuple[str, ...]]
    present: tuple[str, ...]
    empty: tuple[str, ...]
    schema: str = QUERY_SCHEMA

    def get(self, key: str, default: Any = None) -> Any:
        return self.single.get(key, default)

    def getlist(self, key: str) -> list[str]:
        if key in self.multi:
            return list(self.multi[key])
        if key in self.single:
            return [self.single[key]]
        return []

    def to_mapping(self) -> dict[str, Any]:
        result: dict[str, Any] = dict(self.single)
        result.update({key: list(values) for key, values in self.multi.items()})
        return result

    def to_contract(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "single": dict(self.single),
            "multi": {
                key: list(values) for key, values in self.multi.items()
            },
            "present": list(self.present),
            "empty": list(self.empty),
        }


class QueryAdapter:
    """Normalize Mapping, MultiDict, iterable-pair and get-only proxies."""

    def __init__(
        self,
        query: Any,
        *,
        allowed_fields: Iterable[str],
        multi_fields: Iterable[str] = (),
    ) -> None:
        self.query = query
        self.allowed_fields = tuple(dict.fromkeys(str(x) for x in allowed_fields))
        self.multi_fields = frozenset(str(x) for x in multi_fields)
        unknown_multi = self.multi_fields.difference(self.allowed_fields)
        if unknown_multi:
            raise ValueError(
                f"multi_fields 未在 allowed_fields 声明：{sorted(unknown_multi)}"
            )

    def normalize(self) -> NormalizedQuery:
        values: dict[str, list[str]] = {}
        pairs = _iter_pairs(self.query)
        if pairs is not None:
            for key, raw in pairs:
                if key not in self.allowed_fields:
                    continue
                values.setdefault(key, []).append(_text(raw))

        for key in self.allowed_fields:
            getter = getattr(self.query, "getall", None)
            if not callable(getter):
                getter = getattr(self.query, "getlist", None)
            if callable(getter):
                try:
                    raw_values = getter(key)
                except (KeyError, TypeError, ValueError):
                    raw_values = None
                if raw_values is not None:
                    if isinstance(raw_values, (str, bytes)):
                        raw_values = [raw_values]
                    normalized = [_text(item) for item in raw_values]
                    if normalized:
                        values[key] = normalized

        if pairs is None:
            getter = getattr(self.query, "get", None)
            if not callable(getter):
                raise BadRequestError(
                    "服务器无法读取本次查询参数。",
                    code="tavern.query.unsupported_proxy",
                    recovery="请刷新页面后重试；若仍失败，请联系管理员并说明页面与发生时间。",
                )
            sentinel = object()
            for key in self.allowed_fields:
                try:
                    raw = getter(key, sentinel)
                except TypeError:
                    raw = getter(key)
                if raw is not sentinel and raw is not None:
                    values[key] = [_text(raw)]

        single: dict[str, str] = {}
        multi: dict[str, tuple[str, ...]] = {}
        present: list[str] = []
        empty: list[str] = []
        for key in self.allowed_fields:
            items = values.get(key)
            if not items:
                continue
            present.append(key)
            if any(item == "" for item in items):
                empty.append(key)
            if key in self.multi_fields:
                multi[key] = tuple(items)
                continue
            if len(items) > 1:
                raise BadRequestError(
                    f"查询参数“{key}”不能重复。",
                    code="tavern.query.duplicate_single",
                    recovery="请只保留一个值后重试。",
                )
            single[key] = items[0]
        return NormalizedQuery(
            single=single,
            multi=multi,
            present=tuple(present),
            empty=tuple(empty),
        )


def parse_int(
    query: NormalizedQuery,
    key: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = query.get(key)
    if raw in (None, ""):
        return default
    try:
        value = int(str(raw), 10)
    except (TypeError, ValueError) as exc:
        raise BadRequestError(
            f"查询参数“{key}”必须是整数。",
            code="tavern.query.invalid_integer",
            recovery=f"请输入 {minimum} 到 {maximum} 之间的整数。",
        ) from exc
    if value < minimum or value > maximum:
        raise BadRequestError(
            f"查询参数“{key}”超出允许范围。",
            code="tavern.query.out_of_range",
            recovery=f"请输入 {minimum} 到 {maximum} 之间的整数。",
        )
    return value


__all__ = [
    "NormalizedQuery",
    "QUERY_SCHEMA",
    "QueryAdapter",
    "parse_int",
]

