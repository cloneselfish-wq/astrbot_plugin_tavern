from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from ..web.errors import BadRequestError, build_envelope
from ..web.principal import MiniprogramPrincipal
from .auth import MiniProgramTokenService, subject_hash


class _Provider(Protocol):
    async def exchange_code(self, *, provider: str, code: str) -> str: ...


@dataclass(slots=True)
class MiniProgramGateway:
    repository: Any
    provider: _Provider
    token_service: MiniProgramTokenService
    identity_pepper: bytes

    async def login(
        self,
        *,
        provider: str,
        app_id: str,
        code: str,
        display_name: str = "",
    ) -> dict[str, Any]:
        if not str(code or "").strip():
            raise BadRequestError(
                "登录请求缺少 provider code。",
                code="tavern.miniprogram.code_required",
                recovery="请重新发起小程序登录。",
            )
        subject = await self.provider.exchange_code(provider=provider, code=code)
        binding = await self.repository.bind_miniprogram_principal(
            provider=provider,
            app_id=app_id,
            external_subject_hash=subject_hash(
                provider=provider,
                app_id=app_id,
                subject=subject,
                pepper=self.identity_pepper,
            ),
            display_name=display_name,
        )
        token = self.token_service.issue(
            binding_ref=binding["binding_ref"],
            provider=binding["provider"],
        )
        return {
            "schema": "tavern-miniprogram-login/1.0.0-rc10",
            "access_token": token,
            "expires_in": self.token_service.ttl_seconds,
            "principal": binding,
        }

    def principal(self, token: str) -> MiniprogramPrincipal:
        payload = self.token_service.verify(token)
        return MiniprogramPrincipal(
            provider=str(payload.get("provider") or ""),
            binding_ref=str(payload.get("binding_ref") or ""),
        )

    async def join_room(
        self,
        *,
        token: str,
        invite_code: str,
        display_name: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        principal = self.principal(token)
        result = await self.repository.join_room_with_invite(
            binding_ref=principal.binding_ref,
            invite_code=invite_code,
            display_name=display_name,
            idempotency_key=idempotency_key,
        )
        joined_principal = MiniprogramPrincipal(
            provider=principal.provider,
            binding_ref=principal.binding_ref,
            participant_ref=str(result.get("participant_ref") or ""),
            member_role="player",
        )
        return {
            **dict(result),
            "principal": joined_principal.to_public_view(
                session_ref=str(result.get("session_ref") or ""),
            ),
        }

    async def execute(
        self,
        operation: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Thin HTTP-style dispatcher used by the local route harness."""
        try:
            if operation == "login":
                body = await self.login(**dict(payload))
            elif operation == "join_room":
                body = await self.join_room(**dict(payload))
            elif operation == "principal":
                principal = self.principal(str(payload.get("token") or ""))
                body = principal.to_public_view()
            else:
                raise BadRequestError(
                    "小程序请求的操作不存在。",
                    code="tavern.miniprogram.operation_unknown",
                    recovery="请刷新客户端后重试。",
                )
            return {"status": 200, "body": body}
        except Exception as exc:
            envelope = build_envelope(exc)
            return {
                "status": envelope.status_code,
                "body": envelope.to_payload(),
            }
