"""Host-independent ordered delivery for message sequences."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from .player import PlayerMessage, render_player_text


async def send_ordered_parts(
    parts: Sequence[PlayerMessage | str],
    *,
    send: Callable[[PlayerMessage | str], Awaitable[bool]],
    delivered_dedupes: set[str] | None = None,
    before_send: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    receipt_sink: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> tuple[list[PlayerMessage | str], list[dict[str, Any]], set[str]]:
    sequence = [part for part in parts if render_player_text(part).strip()]
    delivered = delivered_dedupes if delivered_dedupes is not None else set()
    unsent: list[PlayerMessage | str] = []
    receipts: list[dict[str, Any]] = []
    for index, part in enumerate(sequence):
        dedupe_key = part.dedupe_key if isinstance(part, PlayerMessage) else ""
        message_type = (
            (part.message_type or part.source or "dynamic")
            if isinstance(part, PlayerMessage)
            else "legacy"
        )
        if dedupe_key and dedupe_key in delivered:
            receipt = {
                "part_index": index,
                "message_type": message_type,
                "status": "deduped",
                "dedupe_key": dedupe_key,
            }
            receipts.append(receipt)
            if receipt_sink is not None:
                await receipt_sink(dict(receipt))
            continue
        pending_receipt = {
            "part_index": index,
            "message_type": message_type,
            "status": "sending",
            "dedupe_key": dedupe_key,
        }
        if before_send is not None:
            await before_send(dict(pending_receipt))
        sent = await send(part)
        receipt = {
            "part_index": index,
            "message_type": message_type,
            "status": "sent" if sent else "failed",
            "dedupe_key": dedupe_key,
        }
        receipts.append(receipt)
        if receipt_sink is not None:
            await receipt_sink(dict(receipt))
        if not sent:
            unsent.extend(sequence[index:])
            break
        if dedupe_key:
            delivered.add(dedupe_key)
    return unsent, receipts, delivered


__all__ = ["send_ordered_parts"]
