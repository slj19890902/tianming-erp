from __future__ import annotations

from contextvars import ContextVar, Token

from starlette.requests import Request


_CURRENT_REQUEST: ContextVar[Request | None] = ContextVar(
    "erp_current_request",
    default=None,
)


def bind_current_request(request: Request) -> Token[Request | None]:
    """Bind one request for downstream synchronous helpers and dependencies."""

    return _CURRENT_REQUEST.set(request)


def reset_current_request(token: Token[Request | None]) -> None:
    _CURRENT_REQUEST.reset(token)


def get_current_request() -> Request | None:
    return _CURRENT_REQUEST.get()
