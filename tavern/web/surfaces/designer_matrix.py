"""Safe presentation projection for the author designer matrix."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .registry import _integer, _mapping, _public_text


def designer_matrix_projection(matrix: Sequence[Any]) -> dict[str, Any]:
    columns = [
        {"label": "专精"},
        {"label": "武器"},
        {"label": "防具"},
        {"label": "能力"},
        {"label": "专长"},
        {"label": "弱点"},
    ]
    field_names = (
        "specializations",
        "weapons",
        "armors",
        "abilities",
        "feats",
        "weaknesses",
    )
    rows: list[dict[str, Any]] = []
    for raw in matrix[:10]:
        item = _mapping(raw)
        label = _public_text(item.get("profession"), limit=100)
        if not label:
            continue
        cells = []
        for name in field_names:
            count = _integer(item.get(name), 0)
            cells.append(
                {
                    "state": "可用" if count > 0 else "缺少",
                    "condition_label": f"{count} 项",
                }
            )
        rows.append({"label": label, "cells": cells})
    return {"columns": columns, "rows": rows} if rows else {}


__all__ = ["designer_matrix_projection"]
