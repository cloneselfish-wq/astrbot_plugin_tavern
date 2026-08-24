from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class MessageSection:
    kind: str
    body: str = ""
    items: list[str] = field(default_factory=list)


@dataclass(slots=True)
class MessageDocument:
    kind: str = "notice"
    title: str = ""
    sections: list[MessageSection] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    audience: str = "public"
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_text(
        cls,
        text: Any,
        *,
        kind: str = "notice",
        audience: str = "public",
    ) -> "MessageDocument":
        return cls(
            kind=kind,
            sections=[
                MessageSection(kind="text", body=str(text or "").strip())
            ],
            audience=audience,
        )
