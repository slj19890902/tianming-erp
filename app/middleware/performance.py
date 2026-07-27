from __future__ import annotations

import logging
import os
from time import perf_counter
from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware


performance_logger = logging.getLogger("erp.performance")


class PerformanceObservabilityMiddleware(BaseHTTPMiddleware):
    """Measure requests without logging query values or response bodies."""

    def __init__(self, app, *, slow_request_ms: float = 500.0) -> None:
        super().__init__(app)
        self.slow_request_ms = max(float(slow_request_ms), 0.0)

    async def dispatch(self, request, call_next):
        started = perf_counter()
        request_id = uuid4().hex
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = (perf_counter() - started) * 1000
            if request.url.path.startswith("/api/"):
                performance_logger.exception(
                    "slow_api method=%s path=%s status=500 duration_ms=%.1f request_id=%s",
                    request.method,
                    request.url.path,
                    duration_ms,
                    request_id,
                )
            raise

        duration_ms = (perf_counter() - started) * 1000
        response.headers["X-Request-ID"] = request_id
        response.headers["Server-Timing"] = f"app;dur={duration_ms:.1f}"
        if request.url.path.startswith("/static/vendor/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        if (
            request.url.path.startswith("/api/")
            and duration_ms >= self.slow_request_ms
        ):
            performance_logger.warning(
                "slow_api method=%s path=%s status=%s duration_ms=%.1f request_id=%s",
                request.method,
                request.url.path,
                response.status_code,
                duration_ms,
                request_id,
            )
        return response


def slow_request_threshold_ms() -> float:
    raw = os.getenv("ERP_SLOW_REQUEST_MS", "500").strip()
    try:
        return max(float(raw), 0.0)
    except ValueError:
        performance_logger.warning(
            "invalid ERP_SLOW_REQUEST_MS=%r; using 500ms",
            raw,
        )
        return 500.0
