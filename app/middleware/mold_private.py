from __future__ import annotations

import logging

from starlette.datastructures import MutableHeaders
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


logger = logging.getLogger(__name__)


class MoldPrivateNoStoreMiddleware:
    """Prevent mold labels and live lookup results from entering caches."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    @staticmethod
    def _matches(path: str) -> bool:
        if path == "/api/warehouse/molds/labels":
            return True
        if path.startswith("/api/warehouse/molds/live/"):
            return True
        if path.startswith("/M/"):
            return True
        prefix = "/api/warehouse/molds/"
        suffix = "/label"
        if not path.startswith(prefix) or not path.endswith("/label"):
            return False
        identity = path[len(prefix) : -len(suffix)].strip("/")
        return bool(identity) and "/" not in identity

    @staticmethod
    def _secure_headers(message: Message) -> None:
        headers = MutableHeaders(scope=message)
        headers["Cache-Control"] = "private, no-store, max-age=0"
        headers["Pragma"] = "no-cache"
        vary = {
            item.strip()
            for item in headers.get("Vary", "").split(",")
            if item.strip()
        }
        vary.add("Cookie")
        headers["Vary"] = ", ".join(sorted(vary))
        headers["X-Robots-Tag"] = "noindex, nofollow"
        headers["Referrer-Policy"] = "no-referrer"
        headers["X-Content-Type-Options"] = "nosniff"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self._matches(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        response_started = False

        async def secure_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                self._secure_headers(message)
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, secure_send)
        except Exception:
            if response_started:
                raise
            logger.exception("模具标签或实时查询发生未处理异常")
            response = PlainTextResponse("Internal Server Error", status_code=500)
            await response(scope, receive, secure_send)
