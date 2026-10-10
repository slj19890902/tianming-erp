from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware


class MoldPrivateNoStoreMiddleware(BaseHTTPMiddleware):
    """Prevent mold labels and live lookup results from entering caches."""

    @staticmethod
    def _matches(path: str) -> bool:
        if path == "/api/warehouse/molds/labels":
            return True
        if path.startswith("/api/warehouse/molds/live/"):
            return True
        if path.startswith("/M/"):
            return True
        prefix = "/api/warehouse/molds/"
        if not path.startswith(prefix) or not path.endswith("/label"):
            return False
        mold_id = path[len(prefix) : -len("/label")].strip("/")
        return mold_id.isdigit() and int(mold_id) > 0

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if not self._matches(request.url.path):
            return response
        response.headers["Cache-Control"] = "private, no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        vary = {
            item.strip()
            for item in response.headers.get("Vary", "").split(",")
            if item.strip()
        }
        vary.add("Cookie")
        response.headers["Vary"] = ", ".join(sorted(vary))
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response
