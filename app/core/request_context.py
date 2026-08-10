from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from threading import Lock

from starlette.requests import Request


_CURRENT_REQUEST: ContextVar[Request | None] = ContextVar(
    "erp_current_request",
    default=None,
)


@dataclass(frozen=True, slots=True)
class RequestPerformanceSnapshot:
    query_count: int
    db_duration_ms: float


@dataclass(slots=True)
class RequestPerformanceStats:
    """Request-local counters; never retain SQL text or parameter values."""

    query_count: int = 0
    db_duration_ms: float = 0.0
    _lock: Lock = field(default_factory=Lock, repr=False)

    def record_database_query(self, duration_ms: float) -> None:
        with self._lock:
            self.query_count += 1
            self.db_duration_ms += max(float(duration_ms), 0.0)

    def snapshot(self) -> RequestPerformanceSnapshot:
        with self._lock:
            return RequestPerformanceSnapshot(
                query_count=self.query_count,
                db_duration_ms=self.db_duration_ms,
            )


def bind_current_request(request: Request) -> Token[Request | None]:
    """Bind one request for downstream synchronous helpers and dependencies."""

    return _CURRENT_REQUEST.set(request)


def reset_current_request(token: Token[Request | None]) -> None:
    _CURRENT_REQUEST.reset(token)


def get_current_request() -> Request | None:
    return _CURRENT_REQUEST.get()


def initialize_request_performance(request: Request) -> RequestPerformanceStats:
    stats = RequestPerformanceStats()
    request.state.performance_stats = stats
    return stats


def get_current_request_performance() -> RequestPerformanceStats | None:
    request = get_current_request()
    if request is None:
        return None
    stats = getattr(request.state, "performance_stats", None)
    return stats if isinstance(stats, RequestPerformanceStats) else None
