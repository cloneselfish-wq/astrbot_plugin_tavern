"""Shared actor choice command used by human and AI controllers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ChoiceCommand:
    session_id: str
    actor_ref: str
    choice_set_id: str
    choice_key: str
    expected_session_revision: int
    idempotency_key: str
    flavor_text: str = ""


__all__ = ["ChoiceCommand"]
