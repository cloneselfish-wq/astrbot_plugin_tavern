from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any, Mapping

from ..web.errors import UnauthorizedError

TOKEN_SCHEMA = "tavern-miniprogram-token/1.0.0-rc10"


def subject_hash(*, provider: str, app_id: str, subject: str, pepper: bytes) -> str:
    payload = "\0".join((str(provider), str(app_id), str(subject))).encode("utf-8")
    return hmac.new(bytes(pepper), payload, hashlib.sha256).hexdigest()


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@dataclass(frozen=True, slots=True)
class MiniProgramTokenService:
    secret: bytes
    audience: str = "astrbot_plugin_tavern:miniprogram"
    ttl_seconds: int = 1200

    def issue(self, *, binding_ref: str, provider: str) -> str:
        now = int(time.time())
        payload = {
            "schema": TOKEN_SCHEMA,
            "aud": self.audience,
            "binding_ref": str(binding_ref),
            "provider": str(provider),
            "iat": now,
            "exp": now + max(60, min(1800, int(self.ttl_seconds))),
            "nonce": secrets.token_hex(8),
        }
        encoded = _b64(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        signature = _b64(
            hmac.new(self.secret, encoded.encode("ascii"), hashlib.sha256).digest()
        )
        return f"{encoded}.{signature}"

    def verify(self, token: str, *, now: int | None = None) -> Mapping[str, Any]:
        try:
            encoded, supplied = str(token or "").split(".", 1)
            expected = _b64(
                hmac.new(
                    self.secret, encoded.encode("ascii"), hashlib.sha256
                ).digest()
            )
            if not hmac.compare_digest(supplied, expected):
                raise ValueError("signature")
            payload = json.loads(_unb64(encoded).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UnauthorizedError(
                "小程序登录状态无效。",
                code="tavern.miniprogram.token_invalid",
                recovery="请重新登录小程序后重试。",
            ) from exc
        current = int(time.time()) if now is None else int(now)
        if payload.get("aud") != self.audience or int(payload.get("exp") or 0) <= current:
            raise UnauthorizedError(
                "小程序登录状态已过期。",
                code="tavern.miniprogram.token_expired",
                recovery="请重新登录小程序后重试。",
            )
        if not str(payload.get("binding_ref") or ""):
            raise UnauthorizedError("小程序登录状态缺少身份绑定。")
        return payload
