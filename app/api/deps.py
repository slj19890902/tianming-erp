from __future__ import annotations

from collections.abc import Generator, Iterable

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.config import load_settings
from app.core.database import SessionLocal
from app.core.security import decode_session_token
from app.models.user import User


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
) -> User:
    current = load_settings()
    token = request.cookies.get(current.session_cookie_name)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未登录或登录已失效",
        )
    try:
        user_id = decode_session_token(token)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(error),
        ) from error

    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未登录或登录已失效",
        )
    return user


class RoleChecker:
    def __init__(self, allowed_roles: Iterable[str]) -> None:
        self.allowed_roles = frozenset(allowed_roles)

    def __call__(
        self,
        current_user: User = Depends(get_current_user),
    ) -> User:
        if current_user.role not in self.allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="权限不足",
            )
        return current_user
