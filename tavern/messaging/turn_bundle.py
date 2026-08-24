"""ordered turn-message contract."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any, Iterable, Literal, Mapping

from ..copy.pagination import paginate_blocks
from ..contracts.narrative_document import chunk_narrative_document
from ..platform_delivery import capabilities_for
from .player import PlayerMessage, render_player_markdown


TurnMessageKind = Literal[
    "actor_status",
    "story",
    "choices",
    "result",
    "notice",
]


def _message_material(message: PlayerMessage) -> Mapping[str, Any]:
    return {
        "message_type": message.message_type,
        "data": dict(message.data),
        "audience": message.audience,
        "privacy": message.privacy,
        "title": message.title,
        "summary": message.summary,
        "sections": list(message.sections),
        "actions": list(message.actions),
        "entities": list(message.entities),
        "pagination_policy": message.pagination_policy,
        "delivery_policy": message.delivery_policy,
        "source_revision": message.source_revision,
    }


def serialize_player_message(message: PlayerMessage) -> dict[str, Any]:
    """Return the complete JSON-safe DTO used by durable part delivery."""

    return {
        "message_type": str(message.message_type or ""),
        "data": dict(message.data),
        "audience": str(message.audience or "public"),
        "title": str(message.title or ""),
        "summary": str(message.summary or ""),
        "sections": list(message.sections),
        "actions": list(message.actions),
        "entities": [dict(item) for item in message.entities],
        "privacy": str(message.privacy or "public"),
        "pagination_policy": str(message.pagination_policy or "logical_blocks"),
        "delivery_policy": str(message.delivery_policy or "group"),
        "source_revision": str(message.source_revision or ""),
        "dedupe_key": str(message.dedupe_key or ""),
        "source": str(message.source or "core"),
        "fallback_text": str(message.fallback_text or ""),
    }


def deserialize_player_message(payload: Mapping[str, Any]) -> PlayerMessage:
    """Restore a persisted message without re-running domain generation."""

    raw_entities = payload.get("entities") or ()
    entities = tuple(
        dict(item) for item in raw_entities if isinstance(item, Mapping)
    )
    return PlayerMessage(
        message_type=str(payload.get("message_type") or ""),
        data=(
            dict(payload.get("data") or {})
            if isinstance(payload.get("data"), Mapping)
            else {}
        ),
        audience=str(payload.get("audience") or "public"),
        title=str(payload.get("title") or ""),
        summary=str(payload.get("summary") or ""),
        sections=tuple(str(item) for item in (payload.get("sections") or ())),
        actions=tuple(str(item) for item in (payload.get("actions") or ())),
        entities=entities,
        privacy=str(payload.get("privacy") or "public"),
        pagination_policy=str(
            payload.get("pagination_policy") or "logical_blocks"
        ),
        delivery_policy=str(payload.get("delivery_policy") or "group"),
        source_revision=str(payload.get("source_revision") or ""),
        dedupe_key=str(payload.get("dedupe_key") or ""),
        source=str(payload.get("source") or "core"),
        fallback_text=str(payload.get("fallback_text") or ""),
    )


def message_digest(message: PlayerMessage) -> str:
    material = json.dumps(
        _message_material(message),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class TurnMessagePart:
    kind: TurnMessageKind
    message: PlayerMessage
    dedupe_key: str
    required: bool = True


@dataclass(frozen=True, slots=True)
class TurnMessageBundle:
    session_id: str
    operation_id: str
    actor_id: str
    state_revision: str
    parts: tuple[TurnMessagePart, ...]

    @property
    def messages(self) -> tuple[PlayerMessage, ...]:
        return tuple(part.message for part in self.parts)

    @classmethod
    def build(
        cls,
        *,
        session_id: str,
        operation_id: str,
        actor_id: str,
        state_revision: str,
        messages: Iterable[tuple[TurnMessageKind, PlayerMessage]],
    ) -> "TurnMessageBundle":
        parts: list[TurnMessagePart] = []
        for kind, raw_message in messages:
            message = replace(
                raw_message,
                source_revision=(
                    raw_message.source_revision or str(state_revision or "")
                ),
            )
            digest = message_digest(message)
            if kind == "actor_status":
                key_material = f"{session_id}:{actor_id}:{kind}:{digest}"
            else:
                key_material = (
                    f"{session_id}:{operation_id}:{state_revision}:{kind}:{digest}"
                )
            dedupe_key = "turn:" + hashlib.sha256(
                key_material.encode("utf-8")
            ).hexdigest()
            message = replace(message, dedupe_key=dedupe_key)
            parts.append(
                TurnMessagePart(
                    kind=kind,
                    message=message,
                    dedupe_key=dedupe_key,
                )
            )
        return cls(
            session_id=str(session_id or ""),
            operation_id=str(operation_id or ""),
            actor_id=str(actor_id or ""),
            state_revision=str(state_revision or ""),
            parts=tuple(parts),
        )


def reply_message_parts(reply: Any) -> tuple[PlayerMessage | str, ...]:
    """Return the structured bundle, then bounded non-turn message fields."""

    bundle = getattr(reply, "message_bundle", None)
    if isinstance(bundle, TurnMessageBundle) and bundle.parts:
        return tuple(part.message for part in bundle.parts)
    messages = tuple(getattr(reply, "messages", ()) or ())
    if messages:
        return messages
    fallback_parts = tuple(
        str(value).strip()
        for value in (
            getattr(reply, "story_text", ""),
            getattr(reply, "turn_text", ""),
        )
        if str(value or "").strip()
    )
    if fallback_parts:
        return fallback_parts
    text = str(getattr(reply, "text", "") or "").strip()
    return (text,) if text else ()


def split_turn_bundle_for_delivery(
    bundle: TurnMessageBundle,
    platform_or_origin: Any,
) -> TurnMessageBundle:
    """Expand an oversized story into receipt-bearing physical BOT parts.

    The split happens after facts are committed but before the delivery run is
    persisted.  Status and choices keep their original order, while every
    story segment receives its own deterministic dedupe key and can therefore
    be resumed without sending an already confirmed segment again.
    """

    maximum = max(256, int(capabilities_for(platform_or_origin).max_text_length))
    messages: list[tuple[TurnMessageKind, PlayerMessage]] = []
    changed = False
    for part in bundle.parts:
        message = part.message
        delivery_document = (
            message.data.get("delivery_narrative_document")
            if isinstance(message.data, Mapping)
            else None
        )
        if part.kind == "story" and isinstance(delivery_document, Mapping):
            pages = chunk_narrative_document(
                delivery_document,
                max(64, maximum - 96),
                include_title=False,
            )
            changed = True
            total = len(pages)
            for index, page in enumerate(pages, start=1):
                segment = replace(
                    message,
                    data={},
                    title=(
                        message.title
                        if total <= 1
                        else f"{message.title}（{index}/{total}）"
                    ),
                    summary=page.text,
                    sections=(),
                    dedupe_key="",
                )
                messages.append(("story", segment))
            continue
        if (
            part.kind != "story"
            or message.message_type
            or message.actions
            or len(render_player_markdown(message)) <= maximum
        ):
            messages.append((part.kind, message))
            continue
        body = "\n\n".join(
            item.strip()
            for item in (message.summary, *message.sections)
            if str(item or "").strip()
        )
        pages = paginate_blocks(body, max(256, maximum - 96))
        if len(pages) <= 1:
            messages.append((part.kind, message))
            continue
        changed = True
        total = len(pages)
        for index, page in enumerate(pages, start=1):
            segment = replace(
                message,
                title=f"{message.title}（{index}/{total}）",
                summary=page,
                sections=(),
                dedupe_key="",
            )
            messages.append(("story", segment))
    if not changed:
        return bundle
    return TurnMessageBundle.build(
        session_id=bundle.session_id,
        operation_id=bundle.operation_id,
        actor_id=bundle.actor_id,
        state_revision=bundle.state_revision,
        messages=messages,
    )


__all__ = [
    "TurnMessageBundle",
    "TurnMessageKind",
    "TurnMessagePart",
    "deserialize_player_message",
    "message_digest",
    "reply_message_parts",
    "serialize_player_message",
    "split_turn_bundle_for_delivery",
]
