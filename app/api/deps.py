from __future__ import annotations

from collections.abc import Generator, Iterable

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import load_settings
from app.core.database import SessionLocal
from app.core.security import decode_session_token
from app.models.access_control import UserCustomerScope
from app.models.user import User


# Permission names are intentionally stable API contracts.  New routes can use
# PermissionChecker without expanding broad role checks across the application.
PERMISSION_CATALOG = frozenset(
    {
        "customers.view",
        "customers.create",
        "customers.edit",
        "customers.deactivate",
        "customers.delete",
        "products.view",
        "products.create",
        "products.edit",
        "products.deactivate",
        "products.delete",
        "quotations.view",
        "quotations.edit",
        "orders.view",
        "orders.create",
        "orders.edit",
        "orders.status",
        "orders.delete",
        "orders.rollback",
        "requisition.view",
        "requisition.execute",
        "dashboard.view",
        "incoming.view",
        "incoming.execute",
        "warehouse.view",
        "warehouse.execute",
        "warehouse.reserve",
        "deliveries.view",
        "deliveries.execute",
        "finance.view",
        "finance.execute",
        "cost.view",
        "system.backup",
        "users.manage",
    }
)

SALES_DEFAULT_PERMISSIONS = frozenset(
    {
        "customers.view",
        "customers.edit",
        "products.view",
        "products.edit",
        "quotations.view",
        "quotations.edit",
        "orders.view",
        "orders.create",
        "orders.edit",
        "dashboard.view",
    }
)
ALL_PERMISSIONS = PERMISSION_CATALOG
ADMIN_ONLY_PERMISSIONS = frozenset({"system.backup", "users.manage"})
BOSS_DEFAULT_PERMISSIONS = frozenset(
    permission
    for permission in ALL_PERMISSIONS
    if not permission.startswith("system.") and permission != "users.manage"
)
ROLE_DEFAULT_PERMISSIONS: dict[str, frozenset[str]] = {
    "admin": ALL_PERMISSIONS,
    "boss": BOSS_DEFAULT_PERMISSIONS,
    "sales": SALES_DEFAULT_PERMISSIONS,
    "finance": frozenset(
        {
            "customers.view",
            "quotations.view",
            "orders.view",
            "dashboard.view",
            "deliveries.view",
            "finance.view",
            "finance.execute",
            "cost.view",
        }
    ),
    "workshop": frozenset(
        {
            "customers.view",
            "products.view",
            "orders.view",
            "dashboard.view",
            "incoming.view",
            "incoming.execute",
            "warehouse.view",
            "warehouse.execute",
            "deliveries.view",
        }
    ),
    "delivery_picker": frozenset({"orders.view"}),
}


def effective_permissions(user: User) -> frozenset[str]:
    """Return role defaults after explicit per-user allow/deny overrides."""
    if user.role == "admin":
        return ALL_PERMISSIONS
    permissions = set(ROLE_DEFAULT_PERMISSIONS.get(user.role, frozenset()))
    for override in user.permission_overrides:
        if override.permission_code not in PERMISSION_CATALOG:
            continue
        if override.is_allowed:
            permissions.add(override.permission_code)
        else:
            permissions.discard(override.permission_code)
    permissions.difference_update(ADMIN_ONLY_PERMISSIONS)
    return frozenset(permissions)


def has_permission(user: User, permission_code: str) -> bool:
    return permission_code in effective_permissions(user)


def customer_scope_ids(user: User, db: Session) -> set[int]:
    return set(
        db.scalars(
            select(UserCustomerScope.customer_id).where(
                UserCustomerScope.user_id == user.id
            )
        ).all()
    )


def has_unrestricted_customer_access(user: User, db: Session) -> bool:
    """Return whether the account can access every customer.

    Existing accounts upgrade with ``customer_access_mode=all`` so the
    migration cannot silently lock them out.  ``selected`` is an explicit
    allow-list mode; an empty selected list intentionally means no customers.
    Admin and boss roles always retain full customer visibility.
    """
    return user.role in {"admin", "boss"} or user.customer_access_mode == "all"


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


class PermissionChecker:
    def __init__(self, permission_code: str) -> None:
        if permission_code not in PERMISSION_CATALOG:
            raise ValueError(f"Unknown permission code: {permission_code}")
        self.permission_code = permission_code

    def __call__(
        self,
        current_user: User = Depends(get_current_user),
    ) -> User:
        if not has_permission(current_user, self.permission_code):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="权限不足",
            )
        return current_user


def require_customer_access(
    customer_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> User:
    """Dependency for endpoints whose resource belongs to one customer."""
    if has_unrestricted_customer_access(current_user, db):
        return current_user
    if customer_id not in customer_scope_ids(current_user, db):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无客户访问权限")
    return current_user
