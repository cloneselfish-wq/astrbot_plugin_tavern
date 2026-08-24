"""Active, content-free health probes for the configured narrative chain."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import time
from typing import Any, Callable, Mapping, Sequence

from .config import TavernConfig


_PROBE_PROMPT = "只回复 OK。"
_PROBE_SYSTEM = "这是连通性检查，不是故事生成。只回复 OK。"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _error_code(exc: BaseException) -> str:
    text = f"{type(exc).__name__} {exc}".lower()
    if (
        "client has been closed" in text
        or "client is closed" in text
        or "closed client" in text
    ):
        return "provider.client_closed"
    if isinstance(exc, TimeoutError) or "timeout" in text or "超时" in text:
        return "provider.timeout"
    if "401" in text or "unauthorized" in text or "authentication" in text:
        return "provider.authentication"
    if "429" in text or "rate" in text or "限流" in text:
        return "provider.rate_limited"
    if "not found" in text or "不存在" in text:
        return "provider.not_found"
    return "provider.unavailable"


def _recovery(code: str) -> str:
    return {
        "provider.client_closed": (
            "模型客户端已经关闭；请重新加载 AstrBot 模型配置或重启 "
            "AstrBot，再运行健康检查。"
        ),
        "provider.timeout": "模型响应超时；已保留配置，请稍后重试健康检查。",
        "provider.authentication": "模型认证失败；请在 AstrBot 模型设置中更新凭据后重试。",
        "provider.rate_limited": "模型服务正在限流；请等待配额恢复或启用备用模型。",
        "provider.not_found": "模型配置已失效；请重新选择已安装的模型提供商。",
        "provider.invalid_response": "模型返回了空响应；请检查模型兼容性后重试。",
        "provider.unavailable": "模型暂不可用；请检查网络、服务状态或备用模型配置。",
    }.get(code, "模型暂不可用；请检查配置后重试。")


@dataclass(frozen=True, slots=True)
class ProviderProbeResult:
    provider_id: str
    role: str
    status: str
    latency_ms: int
    checked_at: str
    error_code: str = ""
    recovery: str = ""

    def to_mapping(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "role": self.role,
            "status": self.status,
            "latency_ms": self.latency_ms,
            "checked_at": self.checked_at,
            "error_code": self.error_code,
            "recovery": self.recovery,
        }


class ProviderHealthService:
    """Probe providers with a bounded, redacted and idempotent workflow."""

    def __init__(
        self,
        *,
        context: Any,
        repository: Any,
        config_provider: Callable[[], TavernConfig],
        per_provider_timeout: float = 10.0,
        total_timeout: float = 30.0,
        cooldown_seconds: int = 60,
    ) -> None:
        self.context = context
        self.repository = repository
        self.config_provider = config_provider
        self.per_provider_timeout = max(0.1, float(per_provider_timeout))
        self.total_timeout = max(
            self.per_provider_timeout,
            float(total_timeout),
        )
        self.cooldown_seconds = max(1, int(cooldown_seconds))
        self._lock = asyncio.Lock()

    def configured_chain(self) -> list[str]:
        config = self.config_provider()
        return list(
            dict.fromkeys(
                item
                for item in (
                    str(config.provider_id or "").strip(),
                    *(
                        str(value or "").strip()
                        for value in config.fallback_provider_ids
                    ),
                )
                if item
            )
        )

    @staticmethod
    def summarize(
        provider_ids: Sequence[str],
        rows: Sequence[Mapping[str, Any]],
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        chain = [str(item or "").strip() for item in provider_ids if str(item or "").strip()]
        if not chain:
            return {
                "status": "not_configured",
                "checked_at": "",
                "items": [],
                "message": "尚未配置固定的主叙事模型。",
            }
        current = now or _now()
        by_id = {str(row.get("provider_id") or ""): row for row in rows}
        items: list[dict[str, Any]] = []
        for index, provider_id in enumerate(chain):
            row = by_id.get(provider_id, {})
            expires = _parse_time(row.get("probe_expires_at"))
            probe_status = str(row.get("probe_status") or "never")
            if expires is not None and expires < current and probe_status not in {"never", "running"}:
                probe_status = "stale"
            items.append(
                {
                    "provider_id": provider_id,
                    "role": "primary" if index == 0 else "fallback",
                    "status": probe_status,
                    "latency_ms": int(row.get("last_probe_latency_ms") or 0),
                    "checked_at": str(row.get("last_probe_at") or ""),
                    "error_code": str(row.get("last_probe_error_code") or ""),
                    "recovery": _recovery(str(row.get("last_probe_error_code") or ""))
                    if str(row.get("last_probe_error_code") or "")
                    else "",
                    "circuit_status": str(row.get("status") or "healthy"),
                }
            )
        statuses = [str(item["status"]) for item in items]
        successes = [status == "healthy" for status in statuses]
        if "running" in statuses:
            status = "running"
            message = "正在检查模型链。"
        elif all(status in {"never", "stale"} for status in statuses):
            status = "never"
            message = "尚未运行有效的模型健康检查。"
        elif any(successes) and all(successes):
            status = "healthy"
            message = "主模型和备用模型最近一次探测均成功。"
        elif any(successes):
            status = "degraded"
            message = "至少一个叙事模型可用，但模型链存在异常。"
        else:
            status = "unavailable"
            message = "最近一次探测未找到可用的叙事模型。"
        checked_at = max(
            (str(item.get("checked_at") or "") for item in items),
            default="",
        )
        return {
            "status": status,
            "checked_at": checked_at,
            "items": items,
            "message": message,
        }

    async def _probe_one(
        self,
        provider_id: str,
        role: str,
        idempotency_key: str,
        expires_at: str,
        semaphore: asyncio.Semaphore,
    ) -> ProviderProbeResult:
        async with semaphore:
            started = time.monotonic()
            checked_at = _iso(_now())
            try:
                response = await asyncio.wait_for(
                    self.context.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=_PROBE_PROMPT,
                        system_prompt=_PROBE_SYSTEM,
                        max_tokens=8,
                        temperature=0,
                    ),
                    timeout=self.per_provider_timeout,
                )
                latency_ms = max(1, int((time.monotonic() - started) * 1000))
                completion = str(
                    getattr(response, "completion_text", "") or ""
                ).strip()
                if not completion:
                    raise ValueError("empty provider response")
                result = ProviderProbeResult(
                    provider_id=provider_id,
                    role=role,
                    status="healthy",
                    latency_ms=latency_ms,
                    checked_at=checked_at,
                )
            except BaseException as exc:
                latency_ms = max(1, int((time.monotonic() - started) * 1000))
                code = (
                    "provider.invalid_response"
                    if isinstance(exc, ValueError)
                    and str(exc) == "empty provider response"
                    else _error_code(exc)
                )
                result = ProviderProbeResult(
                    provider_id=provider_id,
                    role=role,
                    status="unavailable",
                    latency_ms=latency_ms,
                    checked_at=checked_at,
                    error_code=code,
                    recovery=_recovery(code),
                )
            await self.repository.record_provider_result(
                provider_id,
                success=result.status == "healthy",
                probe=True,
                probe_status=result.status,
                probe_latency_ms=result.latency_ms,
                probe_error_code=result.error_code,
                probe_expires_at=expires_at,
                probe_idempotency_key=idempotency_key,
            )
            return result

    async def probe(
        self,
        provider_ids: Sequence[str],
        *,
        idempotency_key: str,
        actor: str,
    ) -> dict[str, Any]:
        del actor  # Actor is accepted for the API/audit contract; no prompt data.
        requested = list(
            dict.fromkeys(
                str(item or "").strip()
                for item in provider_ids
                if str(item or "").strip()
            )
        )
        chain = requested or self.configured_chain()
        if not chain:
            return self.summarize([], [])
        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError("缺少 idempotency_key")
        async with self._lock:
            rows = await self.repository.list_provider_health()
            summary = self.summarize(chain, rows)
            by_id = {str(row.get("provider_id") or ""): row for row in rows}
            now = _now()
            fresh = all(
                (_parse_time(by_id.get(provider_id, {}).get("probe_expires_at")) or datetime.min.replace(tzinfo=timezone.utc))
                > now
                for provider_id in chain
            )
            same_key = all(
                str(by_id.get(provider_id, {}).get("probe_idempotency_key") or "") == key
                for provider_id in chain
            )
            if fresh or same_key and summary["status"] not in {"never", "running"}:
                return {**summary, "cached": True, "idempotency_key": key}
            expires_at = _iso(now + timedelta(seconds=self.cooldown_seconds))
            for provider_id in chain:
                await self.repository.record_provider_result(
                    provider_id,
                    success=False,
                    probe=True,
                    probe_status="running",
                    probe_latency_ms=0,
                    probe_error_code="",
                    probe_expires_at=expires_at,
                    probe_idempotency_key=key,
                )
            semaphore = asyncio.Semaphore(3)
            tasks = [
                self._probe_one(
                    provider_id,
                    "primary" if index == 0 else "fallback",
                    key,
                    expires_at,
                    semaphore,
                )
                for index, provider_id in enumerate(chain)
            ]
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks),
                    timeout=self.total_timeout,
                )
            except TimeoutError:
                # Per-provider tasks normally finish first; the total guard keeps
                # the API bounded if a host call ignores cancellation.
                for task in tasks:
                    if isinstance(task, asyncio.Task):
                        task.cancel()
            rows = await self.repository.list_provider_health()
            return {
                **self.summarize(chain, rows),
                "cached": False,
                "idempotency_key": key,
            }


__all__ = ["ProviderHealthService", "ProviderProbeResult"]
