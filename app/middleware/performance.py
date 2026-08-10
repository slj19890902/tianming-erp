from __future__ import annotations

import logging
import os
from time import perf_counter
from uuid import uuid4

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.request_context import (
    bind_current_request,
    initialize_request_performance,
    reset_current_request,
)


performance_logger = logging.getLogger("erp.performance")


class PerformanceObservabilityMiddleware:
    """Measure requests without logging query values or response bodies."""

    def __init__(self, app: ASGIApp, *, slow_request_ms: float = 500.0) -> None:
        self.app = app
        self.slow_request_ms = max(float(slow_request_ms), 0.0)

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = perf_counter()
        request_id = uuid4().hex
        request = Request(scope, receive=receive)
        request.state.request_id = request_id
        performance_stats = initialize_request_performance(request)
        request_token = bind_current_request(request)
        status_code = 500
        response_bytes = 0
        response_finished = False
        logged = False

        def route_path() -> str:
            route = scope.get("route")
            template = getattr(route, "path", None)
            return str(template) if template else "<unmatched>"

        def write_performance_log(*, error: Exception | None = None) -> None:
            nonlocal logged
            if logged or not str(scope.get("path", "")).startswith("/api/"):
                return
            logged = True
            duration_ms = (perf_counter() - started) * 1000
            snapshot = performance_stats.snapshot()
            fields = (
                scope.get("method", ""),
                route_path(),
                status_code,
                duration_ms,
                snapshot.query_count,
                snapshot.db_duration_ms,
                response_bytes,
                request_id,
            )
            if error is not None:
                performance_logger.error(
                    "api_error method=%s path=%s status=%s duration_ms=%.1f "
                    "query_count=%s db_duration_ms=%.1f response_bytes=%s "
                    "request_id=%s db_scope=sqlalchemy error_type=%s",
                    *fields,
                    type(error).__name__,
                )
            elif status_code >= 500:
                performance_logger.error(
                    "api_error method=%s path=%s status=%s duration_ms=%.1f "
                    "query_count=%s db_duration_ms=%.1f response_bytes=%s "
                    "request_id=%s db_scope=sqlalchemy",
                    *fields,
                )
            elif duration_ms >= self.slow_request_ms:
                performance_logger.warning(
                    "slow_api method=%s path=%s status=%s duration_ms=%.1f "
                    "query_count=%s db_duration_ms=%.1f response_bytes=%s "
                    "request_id=%s db_scope=sqlalchemy",
                    *fields,
                )

        async def send_with_observability(message: Message) -> None:
            nonlocal status_code, response_bytes, response_finished
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                snapshot = performance_stats.snapshot()
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                headers["Server-Timing"] = (
                    f"app;dur={(perf_counter() - started) * 1000:.1f}, "
                    f"db;dur={snapshot.db_duration_ms:.1f}"
                )
                if str(scope.get("path", "")).startswith("/static/vendor/"):
                    headers["Cache-Control"] = (
                        "public, max-age=31536000, immutable"
                    )
            elif message["type"] == "http.response.body":
                response_bytes += len(message.get("body", b""))

            await send(message)

            if (
                message["type"] == "http.response.body"
                and not message.get("more_body", False)
            ):
                response_finished = True
                write_performance_log()

        try:
            await self.app(scope, receive, send_with_observability)
            if not response_finished:
                write_performance_log()
        except Exception as error:
            write_performance_log(error=error)
            raise
        finally:
            reset_current_request(request_token)


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
