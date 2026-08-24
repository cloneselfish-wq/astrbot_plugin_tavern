"""Short-lived opaque keys and signed cursors for visual projections.

The visual API must correlate nodes and actions without returning database,
platform, provider, or protocol identifiers.  Keys are therefore HMACs under a
per-process secret: stable for the lifetime of one Tavern process, rotated on
restart, and not reversible from the response.  Pagination cursors use the
same boundary and never expose a row id or raw sequence by themselves.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass, field


_PROCESS_SECRET = secrets.token_bytes(32)


def _token(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


@dataclass(frozen=True, slots=True)
class OpaqueKeyFactory:
    """Create response-safe object keys and tamper-evident cursors."""

    scope: str
    secret: bytes = field(default=_PROCESS_SECRET, repr=False)

    def key(self, kind: str, value: object) -> str:
        label = "".join(
            char for char in str(kind or "object").lower() if char.isalnum()
        )[:12] or "object"
        material = f"{self.scope}\x1f{label}\x1f{value}".encode("utf-8")
        digest = hmac.new(self.secret, material, hashlib.sha256).digest()[:9]
        return f"{label}_{_token(digest)}"

    def cursor(self, kind: str, position: int) -> str:
        position = max(0, int(position))
        payload = f"1:{kind}:{position}".encode("ascii")
        signature = hmac.new(
            self.secret,
            self.scope.encode("utf-8") + b"\x1f" + payload,
            hashlib.sha256,
        ).digest()[:10]
        return "cursor_" + _token(payload + b"." + signature)

    def anchor_cursor(self, kind: str, value: object) -> str:
        """Return a non-reversible cursor anchored to one stable source row."""

        material = (
            f"{self.scope}\x1fanchor\x1f{str(kind)}\x1f{str(value)}"
        ).encode("utf-8")
        digest = hmac.new(self.secret, material, hashlib.sha256).digest()[:16]
        return "cursor_anchor_" + _token(digest)

    def after_anchor(
        self,
        kind: str,
        cursor: object,
        values: list[object] | tuple[object, ...],
    ) -> int:
        """Locate a signed row anchor and return the following page offset."""

        token = str(cursor or "").strip()
        if not token:
            return 0
        if not token.startswith("cursor_anchor_"):
            raise ValueError("分页位置已经失效")
        for index, value in enumerate(values):
            candidate = self.anchor_cursor(kind, value)
            if hmac.compare_digest(candidate, token):
                return index + 1
        raise ValueError("分页位置已经失效")

    def read_cursor(self, kind: str, value: object) -> int:
        text = str(value or "").strip()
        if not text:
            return 0
        if not text.startswith("cursor_"):
            raise ValueError("分页位置已经失效")
        encoded = text[len("cursor_") :]
        encoded += "=" * (-len(encoded) % 4)
        try:
            raw = base64.urlsafe_b64decode(encoded.encode("ascii"))
            # The HMAC is arbitrary binary and may itself contain ``b"."``.
            # Split by the fixed signature width instead of searching from the
            # right, otherwise a valid cursor fails nondeterministically.
            if len(raw) < 12 or raw[-11:-10] != b".":
                raise ValueError
            payload = raw[:-11]
            signature = raw[-10:]
        except (ValueError, TypeError, UnicodeError) as exc:
            raise ValueError("分页位置已经失效") from exc
        expected = hmac.new(
            self.secret,
            self.scope.encode("utf-8") + b"\x1f" + payload,
            hashlib.sha256,
        ).digest()[:10]
        if not hmac.compare_digest(signature, expected):
            raise ValueError("分页位置已经失效")
        try:
            version, cursor_kind, raw_position = payload.decode("ascii").split(
                ":", 2
            )
            position = int(raw_position)
        except (ValueError, UnicodeError) as exc:
            raise ValueError("分页位置已经失效") from exc
        if version != "1" or cursor_kind != str(kind) or position < 0:
            raise ValueError("分页位置已经失效")
        return position


__all__ = ["OpaqueKeyFactory"]
