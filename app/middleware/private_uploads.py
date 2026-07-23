from __future__ import annotations

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware


class PrivateUploadGuardMiddleware(BaseHTTPMiddleware):
    """Never let the generic static mount publish ERP upload directories."""

    async def dispatch(self, request, call_next):
        normalized_path = request.url.path.rstrip("/")
        if normalized_path == "/static/uploads" or normalized_path.startswith(
            "/static/uploads/"
        ):
            return JSONResponse(status_code=404, content={"detail": "文件不存在"})
        return await call_next(request)
