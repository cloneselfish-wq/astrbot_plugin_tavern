from __future__ import annotations

import asyncio
import json
import shutil
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, BinaryIO, Iterator, Mapping


def _entry_module():
    module = sys.modules.get(f"{__package__}.web_console")
    if module is None:
        raise RuntimeError("web console entry module is not loaded")
    return module


@dataclass(slots=True)
class StandaloneUploadFile:
    """Upload object used by the independent panel request adapter."""

    filename: str
    stream: BinaryIO

    async def save(self, path: str | Path) -> None:
        target = Path(path)

        def _copy() -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            self.stream.seek(0)
            with target.open("wb") as output:
                shutil.copyfileobj(self.stream, output, length=1024 * 1024)

        await asyncio.to_thread(_copy)

    def close(self) -> None:
        try:
            self.stream.close()
        except Exception:
            pass


class StandaloneQueryParams(dict[str, Any]):
    """Small MultiDict-compatible query adapter used by console handlers."""

    def get(
        self,
        key: str,
        default: Any = None,
        type: Any = None,
    ) -> Any:
        value = super().get(key, default)
        if type is None:
            return value
        try:
            return type(value)
        except (TypeError, ValueError):
            return default


@dataclass(slots=True)
class StandaloneRequest:
    """Minimal AstrBot Web request contract for the self-hosted console."""

    username: str
    method: str
    query: StandaloneQueryParams = field(default_factory=StandaloneQueryParams)
    payload: Mapping[str, Any] = field(default_factory=dict)
    uploads: Mapping[str, StandaloneUploadFile] = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)
    auth_source: str = "remote_panel"
    invalid: bool = False

    async def json(self, default: Any = None) -> Any:
        return dict(self.payload) if self.payload else default

    async def files(self) -> dict[str, StandaloneUploadFile]:
        return dict(self.uploads)

    def close(self) -> None:
        for upload in self.uploads.values():
            upload.close()


@dataclass(slots=True)
class StandaloneResponse:
    """Framework-neutral response consumed by ``RemotePanelServer``."""

    kind: str
    payload: Any = None
    status_code: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    filename: str = ""
    content_type: str = ""
    path: Path | None = None
    stream: AsyncIterator[Any] | None = None


_standalone_request: ContextVar[StandaloneRequest | None] = ContextVar(
    "tavern_standalone_web_request",
    default=None,
)


@contextmanager
def standalone_request_context(
    value: StandaloneRequest,
) -> Iterator[StandaloneRequest]:
    token = _standalone_request.set(value)
    try:
        yield value
    finally:
        _standalone_request.reset(token)


def current_standalone_request() -> StandaloneRequest | None:
    return _standalone_request.get()


def is_standalone_upload(value: Any) -> bool:
    return isinstance(value, StandaloneUploadFile)


class _RequestProxy:
    def __getattr__(self, name: str) -> Any:
        current = current_standalone_request()
        if current is not None:
            return getattr(current, name)
        return getattr(_entry_module().request, name)

    def __setattr__(self, name: str, value: Any) -> None:
        current = current_standalone_request()
        if current is not None:
            setattr(current, name, value)
            return
        setattr(_entry_module().request, name, value)


request = _RequestProxy()


def json_response(
    payload: Any,
    status_code: int = 200,
    **kwargs: Any,
):
    if current_standalone_request() is not None:
        headers = {
            str(key): str(value)
            for key, value in dict(kwargs.get("headers") or {}).items()
        }
        return StandaloneResponse(
            kind="json",
            payload=payload,
            status_code=int(status_code or 200),
            headers=headers,
            content_type="application/json; charset=utf-8",
        )
    return _entry_module().json_response(
        payload,
        status_code=status_code,
        **kwargs,
    )


def plugin_page_json_response(
    payload: Any,
    status_code: int = 200,
    **kwargs: Any,
):
    """Return data without losing its outer envelope in AstrBot's page bridge.

    AstrBot's plugin-page parent deliberately resolves successful extension
    responses as ``response.data.data ?? response.data``.  Tavern surface and
    visualization envelopes also have a top-level ``data`` member, so returning
    those envelopes directly makes the real host discard ``state``, ``summary``,
    ``permissions`` and the outer ``data`` key before the iframe receives them.

    The independent panel does not perform that host-side unwrap and therefore
    keeps the historical raw response.  Native plugin-page success responses
    get exactly one conventional AstrBot ``{status, data}`` wrapper; HTTP errors
    remain unchanged so their existing status/problem handling is preserved.
    """

    status = int(status_code or 200)
    if current_standalone_request() is None and 200 <= status < 300:
        payload = {"status": "ok", "data": payload}
    return json_response(payload, status_code=status, **kwargs)


def plugin_page_surface_response(
    payload: Any,
    status_code: int = 200,
    **kwargs: Any,
):
    """Carry a safe VisualEnvelope through AstrBot's plugin-page bridge.

    AstrBot's current bridge rejects non-2xx extension responses before the
    iframe can read their structured ``problems`` entry.  Read-only surface
    endpoints already return a privacy-filtered VisualEnvelope for validation,
    conflict and server failures, so native plugin pages transport those
    envelopes as a successful bridge message.  Authentication and
    authorization remain real 401/403 responses, and the standalone HTTP panel
    retains every original status code.
    """

    status = int(status_code or 200)
    if current_standalone_request() is None and status not in {401, 403}:
        status = 200
    return plugin_page_json_response(payload, status_code=status, **kwargs)


def error_response(
    message: Any,
    status_code: int = 400,
    **kwargs: Any,
):
    if current_standalone_request() is not None:
        payload = message if isinstance(message, Mapping) else {"error": str(message)}
        return json_response(payload, status_code=status_code, **kwargs)
    return _entry_module().error_response(
        message,
        status_code=status_code,
        **kwargs,
    )


def file_response(
    path: str | Path,
    *,
    filename: str = "",
    content_type: str = "application/octet-stream",
    **kwargs: Any,
):
    if current_standalone_request() is not None:
        return StandaloneResponse(
            kind="file",
            path=Path(path),
            filename=str(filename or Path(path).name),
            content_type=str(content_type or "application/octet-stream"),
            status_code=int(kwargs.get("status_code") or 200),
        )
    return _entry_module().file_response(
        path,
        filename=filename,
        content_type=content_type,
        **kwargs,
    )


def stream_response(stream: AsyncIterator[Any], **kwargs: Any):
    if current_standalone_request() is not None:
        return StandaloneResponse(
            kind="stream",
            stream=stream,
            status_code=int(kwargs.get("status_code") or 200),
            content_type=str(
                kwargs.get("content_type") or "text/event-stream; charset=utf-8"
            ),
            headers={
                str(key): str(value)
                for key, value in dict(kwargs.get("headers") or {}).items()
            },
        )
    return _entry_module().stream_response(stream, **kwargs)


def standalone_json_bytes(response: StandaloneResponse) -> bytes:
    return json.dumps(
        response.payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


__all__ = [
    "StandaloneRequest",
    "StandaloneResponse",
    "StandaloneQueryParams",
    "StandaloneUploadFile",
    "current_standalone_request",
    "error_response",
    "file_response",
    "is_standalone_upload",
    "json_response",
    "plugin_page_json_response",
    "plugin_page_surface_response",
    "request",
    "standalone_json_bytes",
    "standalone_request_context",
    "stream_response",
]
