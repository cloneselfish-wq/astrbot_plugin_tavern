from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from ..web.errors import UnauthorizedError


@dataclass(slots=True)
class MiniProgramProviderStub:
    """Deterministic local substitute for WeChat/QQ code exchange."""

    identities: Mapping[str, str] = field(default_factory=dict)
    consumed_codes: set[str] = field(default_factory=set)

    async def exchange_code(self, *, provider: str, code: str) -> str:
        key = f"{str(provider).lower()}:{str(code)}"
        if key in self.consumed_codes:
            raise UnauthorizedError(
                "登录凭证已经使用。",
                code="tavern.miniprogram.code_replayed",
                recovery="请重新获取登录凭证。",
            )
        subject = str(self.identities.get(key) or "")
        if not subject:
            raise UnauthorizedError(
                "登录凭证无效或已过期。",
                code="tavern.miniprogram.code_invalid",
                recovery="请重新登录小程序。",
            )
        self.consumed_codes.add(key)
        return subject
