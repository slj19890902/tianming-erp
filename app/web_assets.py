from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Final

from fastapi import Request
from fastapi.responses import FileResponse, Response
from starlette.datastructures import Headers
from starlette.middleware.gzip import GZipMiddleware, GZipResponder
from starlette.types import Message, Receive, Scope, Send


_NON_COMPRESSIBLE_MEDIA_PREFIXES: Final = ("image/", "audio/", "video/", "multipart/")
_NON_COMPRESSIBLE_MEDIA_TYPES: Final = frozenset(
    {
        "application/octet-stream",
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/x-7z-compressed",
        "application/x-rar-compressed",
        "application/x-zip-compressed",
        "application/zip",
        "text/event-stream",
    }
)


def _response_should_bypass_gzip(message: Message) -> bool:
    if int(message.get("status", 200)) == 206:
        return True
    headers = Headers(raw=message.get("headers", []))
    content_type = headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type in _NON_COMPRESSIBLE_MEDIA_TYPES:
        return True
    if content_type.startswith(_NON_COMPRESSIBLE_MEDIA_PREFIXES):
        return True
    return headers.get("content-disposition", "").lstrip().lower().startswith("attachment")


class _SelectiveGZipResponder(GZipResponder):
    """Keep binary downloads and partial responses byte-for-byte stable."""

    bypass_gzip = False

    async def send_with_gzip(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            self.bypass_gzip = _response_should_bypass_gzip(message)
        if self.bypass_gzip:
            await self.send(message)
            return
        await super().send_with_gzip(message)


class SelectiveGZipMiddleware(GZipMiddleware):
    """Compress text/API payloads without changing Range or binary downloads."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = Headers(scope=scope)
            if "gzip" in headers.get("accept-encoding", "") and "range" not in headers:
                responder = _SelectiveGZipResponder(
                    self.app,
                    self.minimum_size,
                    compresslevel=self.compresslevel,
                )
                await responder(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _etag_matches(if_none_match: str, etag: str) -> bool:
    """Use weak comparison for GET/HEAD cache revalidation."""

    expected = etag.removeprefix("W/")
    for candidate in if_none_match.split(","):
        token = candidate.strip()
        if token == "*" or token.removeprefix("W/") == expected:
            return True
    return False


def conditional_file_response(
    request: Request,
    path: str | Path,
    *,
    headers: Mapping[str, str] | None = None,
) -> FileResponse | Response:
    """Serve a local shell file with explicit revalidation and a real 304 path."""

    file_path = Path(path)
    response = FileResponse(
        file_path,
        headers=headers,
        stat_result=file_path.stat(),
    )
    response.headers.setdefault("Cache-Control", "no-cache")

    etag = response.headers.get("etag")
    if_none_match = request.headers.get("if-none-match")
    if (
        request.method in {"GET", "HEAD"}
        and etag
        and if_none_match
        and _etag_matches(if_none_match, etag)
    ):
        not_modified_headers = {
            "ETag": etag,
            "Cache-Control": response.headers["Cache-Control"],
            "Vary": "Accept-Encoding",
        }
        if last_modified := response.headers.get("last-modified"):
            not_modified_headers["Last-Modified"] = last_modified
        return Response(status_code=304, headers=not_modified_headers)

    return response
