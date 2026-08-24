from __future__ import annotations

from typing import Any

from .registry import _text


def _job_type(value: Any) -> str:
    return {
        "playtest": "试玩报告",
        "semantic_diff": "语义差异",
        "full_preflight": "发布前完整检查",
        "world_health": "世界体检",
        "world_validate": "世界验证",
        "world_build": "构建世界",
        "world_export": "导出世界",
        "package_build": "生成安装产物",
        "publish": "发布世界",
    }.get(_text(value, limit=80).lower(), "作者任务")


__all__ = ["_job_type"]
