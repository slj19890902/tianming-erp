from collections.abc import Generator, Iterable

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import load_settings
from app.core.database import SessionLocal
from app.core.request_context import get_current_request
from app.core.security import decode_session_token
from app.models.access_control import UserCustomerScope
from app.models.user import User
from app.services.audit_log import append_audit_event


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
        "contracts.view",
        "contracts.edit",
        "contracts.convert",
        "orders.view",
        "orders.create",
        "orders.edit",
        "orders.status",
        "orders.delete",
        "orders.rollback",
        "production.printing.view",
        "production.die_cut.view",
        "requisition.view",
        "requisition.execute",
        "requisition.purchase_price.confirm",
        "requisition.purchase_price.correct",
        "dashboard.view",
        "incoming.view",
        "incoming.execute",
        "incoming.material_variance.confirm",
        "warehouse.view",
        "warehouse.execute",
        "warehouse.archive",
        "warehouse.correct",
        "warehouse.reserve",
        "warehouse.stocktake.view",
        "warehouse.stocktake.submit",
        "warehouse.stocktake.review",
        "deliveries.view",
        "deliveries.execute",
        "deliveries.over_delivery",
        "deliveries.pick",
        "finance.view",
        "finance.execute",
        "finance.return_receipt.period.adjust",
        "finance.statement.confirm",
        "finance.invoice_task.generate",
        "finance.invoice_result.register",
        "finance.invoice_profile.manage",
        "finance.invoice_attachment.view",
        "finance.invoice_attachment.manage",
        "cost.view",
        "pdf_training.view",
        "pdf_training.manage",
        "system.backup",
        "users.manage",
        "audit.view",
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
        "contracts.view",
        "contracts.edit",
        "contracts.convert",
        "orders.view",
        "orders.create",
        "orders.edit",
        "dashboard.view",
    }
)
ALL_PERMISSIONS = PERMISSION_CATALOG
ADMIN_ONLY_PERMISSIONS = frozenset(
    {
        "system.backup",
        "users.manage",
        "pdf_training.manage",
        "warehouse.stocktake.review",
        "audit.view",
    }
)
BOSS_DEFAULT_PERMISSIONS = frozenset(
    permission
    for permission in ALL_PERMISSIONS
    if not permission.startswith(("system.", "pdf_training."))
    and permission != "users.manage"
    and permission != "deliveries.over_delivery"
    and permission
    not in {
        "finance.statement.confirm",
        "finance.invoice_task.generate",
        "finance.invoice_result.register",
        "finance.invoice_profile.manage",
        "finance.invoice_attachment.manage",
    }
    and permission not in ADMIN_ONLY_PERMISSIONS
)
ROLE_DEFAULT_PERMISSIONS: dict[str, frozenset[str]] = {
    "admin": ALL_PERMISSIONS,
    "boss": BOSS_DEFAULT_PERMISSIONS,
    "sales": SALES_DEFAULT_PERMISSIONS,
    "finance": frozenset(
        {
            "customers.view",
            "quotations.view",
            "contracts.view",
            "orders.view",
            "dashboard.view",
            "deliveries.view",
            "finance.view",
            "finance.execute",
            "finance.return_receipt.period.adjust",
            "finance.statement.confirm",
            "finance.invoice_task.generate",
            "finance.invoice_result.register",
            "finance.invoice_profile.manage",
            "finance.invoice_attachment.view",
            "finance.invoice_attachment.manage",
            "cost.view",
        }
    ),
    "workshop": frozenset(
        {
            "customers.view",
            "products.view",
            "orders.view",
            "production.printing.view",
            "production.die_cut.view",
            "dashboard.view",
            "incoming.view",
            "incoming.execute",
            "warehouse.view",
            "warehouse.execute",
            "warehouse.stocktake.view",
            "warehouse.stocktake.submit",
            "deliveries.view",
        }
    ),
    "delivery_picker": frozenset({"deliveries.pick"}),
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
        user_id, token_auth_version = decode_session_token(token)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(error),
        ) from error

    user = db.get(User, user_id)
    if (
        user is None
        or not user.is_active
        or token_auth_version != user.auth_version
    ):
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
        request: Request,
        current_user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> User:
        if current_user.role not in self.allowed_roles:
            _record_security_denial(
                db,
                request=request,
                current_user=current_user,
                action_code="role.denied",
                object_ref=",".join(sorted(self.allowed_roles)),
                details={"allowed_roles": sorted(self.allowed_roles)},
            )
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
        request: Request,
        current_user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> User:
        if not has_permission(current_user, self.permission_code):
            _record_security_denial(
                db,
                request=request,
                current_user=current_user,
                action_code="permission.denied",
                object_ref=self.permission_code,
                details={"permission_code": self.permission_code},
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="权限不足",
            )
        return current_user


def _record_security_denial(
    db: Session,
    *,
    request: Request | None,
    current_user: User,
    action_code: str,
    object_ref: str,
    details: dict[str, object],
) -> None:
    """Best-effort security evidence that must never turn a denial into access.

    A separate short transaction prevents a rejected access check from
    committing or rolling back any business work already present in the
    caller's session.  Audit failure is swallowed only to preserve the
    fail-closed 403 response; it can never authorize the request.
    """
    request = request or get_current_request()
    try:
        with Session(bind=db.get_bind()) as audit_db:
            append_audit_event(
                audit_db,
                request=request,
                actor=current_user,
                event_category="security",
                result="denied",
                source=(
                    "mobile"
                    if request is not None
                    and request.url.path.startswith("/api/mobile/")
                    else "web"
                ),
                module_code="security",
                action_code=action_code,
                resource="AccessControl",
                entity_type="permission",
                object_ref=object_ref,
                description="权限校验拒绝",
                details={
                    **details,
                    "method": request.method if request is not None else None,
                    "path": request.url.path if request is not None else None,
                },
            )
            audit_db.commit()
    except Exception:
        pass


def require_customer_access(
    customer_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    request: Request = None,
) -> User:
    """Dependency for endpoints whose resource belongs to one customer."""
    if has_unrestricted_customer_access(current_user, db):
        return current_user
    if customer_id not in customer_scope_ids(current_user, db):
        _record_security_denial(
            db,
            request=request,
            current_user=current_user,
            action_code="customer_scope.denied",
            object_ref=str(customer_id),
            details={"customer_id": customer_id},
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无客户访问权限")
    return current_user
