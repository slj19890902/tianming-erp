from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.api.deps import (
    ADMIN_ONLY_PERMISSIONS,
    PERMISSION_CATALOG,
    ROLE_DEFAULT_PERMISSIONS,
    RoleChecker,
    customer_scope_ids,
    has_unrestricted_customer_access,
    effective_permissions,
    get_current_user,
    get_db,
)
from app.core.config import load_settings
from app.core.security import create_session_token, hash_password, verify_password
from app.models.access_control import UserCustomerScope, UserPermissionOverride
from app.models.audit import OperationLog
from app.models.customer import Customer
from app.models.user import USER_ROLES, User


router = APIRouter()


PERMISSION_LABELS: dict[str, tuple[str, str]] = {
    "customers.view": ("customers", "客户查看"),
    "customers.create": ("customers", "客户新增"),
    "customers.edit": ("customers", "客户编辑"),
    "customers.deactivate": ("customers", "客户停用"),
    "customers.delete": ("customers", "客户删除"),
    "products.view": ("products", "产品查看"),
    "products.create": ("products", "产品新增"),
    "products.edit": ("products", "产品编辑"),
    "products.deactivate": ("products", "产品停用"),
    "products.delete": ("products", "产品删除"),
    "quotations.view": ("quotations", "报价查看"),
    "quotations.edit": ("quotations", "报价编辑"),
    "orders.view": ("orders", "订单查看"),
    "orders.create": ("orders", "订单新增"),
    "orders.edit": ("orders", "订单编辑"),
    "orders.status": ("orders", "订单状态操作"),
    "orders.delete": ("orders", "订单删除"),
    "orders.rollback": ("orders", "订单回滚"),
    "requisition.view": ("requisition", "报料查看"),
    "requisition.execute": ("requisition", "报料执行"),
    "dashboard.view": ("dashboard", "首页仪表盘查看"),
    "incoming.view": ("incoming", "来料入库查看"),
    "incoming.execute": ("incoming", "来料入库操作"),
    "warehouse.view": ("warehouse", "仓库库存查看"),
    "warehouse.execute": ("warehouse", "仓库库存操作"),
    "warehouse.reserve": ("warehouse", "订单库存预占"),
    "deliveries.view": ("deliveries", "送货查看"),
    "deliveries.execute": ("deliveries", "送货操作"),
    "finance.view": ("finance", "财务查看"),
    "finance.execute": ("finance", "财务操作"),
    "cost.view": ("sensitive", "成本/毛利查看"),
    "system.backup": ("system", "系统备份"),
    "users.manage": ("system", "用户权限管理"),
}


class LoginRequest(BaseModel):
    username: str
    password: str
    remember_me: bool = False


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class ResetPasswordRequest(BaseModel):
    new_password: str


class UserCreateRequest(BaseModel):
    username: str
    password: str
    role: str
    real_name: str
    display_name: str | None = None
    is_active: bool = True
    must_change_password: bool = True


class UserUpdateRequest(BaseModel):
    username: str | None = None
    password: str | None = None
    role: str | None = None
    real_name: str | None = None
    display_name: str | None = None
    is_active: bool | None = None
    must_change_password: bool | None = None


class PermissionOverridesRequest(BaseModel):
    overrides: dict[str, bool] = Field(default_factory=dict)


class CustomerScopesRequest(BaseModel):
    mode: Literal["all", "selected"] = "all"
    customer_ids: list[int] = Field(default_factory=list)


class UserAccessRequest(CustomerScopesRequest):
    overrides: dict[str, bool] = Field(default_factory=dict)


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    role: str
    real_name: str
    display_name: str | None
    must_change_password: bool
    customer_access_mode: str


def _user_payload(user: User) -> dict:
    return UserResponse.model_validate(user).model_dump()


def _auth_payload(user: User, db: Session) -> dict:
    return {
        "ok": True,
        "user": _user_payload(user),
        "permissions": sorted(effective_permissions(user)),
        "customer_scope": sorted(customer_scope_ids(user, db)),
        "customer_access_mode": user.customer_access_mode,
        "unrestricted_customer_access": has_unrestricted_customer_access(user, db),
    }


def _permission_catalog_payload(user: User) -> list[dict]:
    role_defaults = ROLE_DEFAULT_PERMISSIONS.get(user.role, frozenset())
    rows = []
    for code in sorted(PERMISSION_CATALOG):
        module, name = PERMISSION_LABELS.get(code, (code.split(".", 1)[0], code))
        rows.append(
            {
                "code": code,
                "module": module,
                "name": name,
                "default_allowed": code in role_defaults,
                "locked": user.role == "admin"
                or (user.role != "admin" and code in ADMIN_ONLY_PERMISSIONS),
            }
        )
    return rows


def _get_user_or_404(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    return user


def _validate_role(role: str) -> str:
    normalized = role.strip()
    if normalized not in USER_ROLES:
        raise HTTPException(status_code=400, detail="角色无效")
    return normalized


def _validate_username(username: str) -> str:
    normalized = username.strip()
    if not normalized:
        raise HTTPException(status_code=400, detail="用户名不能为空")
    if len(normalized) > 50:
        raise HTTPException(status_code=400, detail="用户名长度不能超过 50")
    return normalized


def _validate_new_password(password: str) -> None:
    if len(password) < 10:
        raise HTTPException(status_code=400, detail="新密码至少需要 10 位")
    if not any(char.isalpha() for char in password) or not any(
        char.isdigit() for char in password
    ):
        raise HTTPException(status_code=400, detail="新密码必须同时包含字母和数字")


def _password_log(
    *,
    actor: User,
    target: User,
    action: str,
    request: Request,
) -> OperationLog:
    return OperationLog(
        user_id=actor.id,
        action=action,
        resource="User",
        details=json.dumps(
            {"username": target.username, "target_user_id": target.id},
            ensure_ascii=False,
        ),
        ip_address=request.client.host if request.client else None,
        username=actor.username,
        role=actor.role,
        entity_type="user",
        entity_id=target.id,
        description="修改密码" if action == "CHANGE_PASSWORD" else "管理员重置密码",
        user_agent=request.headers.get("user-agent"),
    )


@router.post("/login")
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> dict:
    username = payload.username.strip()
    user = db.scalar(select(User).where(User.username == username))
    if (
        user is None
        or not user.is_active
        or not verify_password(payload.password, user.password_hash)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
        )

    current = load_settings()
    remember_seconds = 30 * 24 * 60 * 60
    token = create_session_token(
        user.id,
        expires_minutes=(
            remember_seconds // 60
            if payload.remember_me
            else current.session_expire_minutes
        ),
    )
    cookie_options = {
        "key": current.session_cookie_name,
        "value": token,
        "httponly": True,
        "secure": current.session_cookie_secure,
        "samesite": "lax",
        "path": "/",
    }
    if payload.remember_me:
        cookie_options["max_age"] = remember_seconds
    response.set_cookie(**cookie_options)
    db.add(
        OperationLog(
            user_id=user.id,
            action="LOGIN",
            resource="User",
            details=json.dumps(
                {"username": user.username, "role": user.role},
                ensure_ascii=False,
            ),
            ip_address=request.client.host if request.client else None,
            username=user.username,
            role=user.role,
            entity_type="user",
            entity_id=user.id,
            description="用户登录",
            user_agent=request.headers.get("user-agent"),
        )
    )
    db.commit()
    return _auth_payload(user, db)


@router.post("/logout")
def logout(response: Response) -> dict[str, bool]:
    current = load_settings()
    response.delete_cookie(
        key=current.session_cookie_name,
        path="/",
        secure=current.session_cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return {"ok": True}


@router.get("/me")
def me(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    return _auth_payload(current_user, db)


@router.put("/password")
def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    if not verify_password(payload.current_password, current_user.password_hash):
        raise HTTPException(status_code=400, detail="当前密码错误")
    _validate_new_password(payload.new_password)
    if verify_password(payload.new_password, current_user.password_hash):
        raise HTTPException(status_code=400, detail="新密码不能与当前密码相同")

    current_user.password_hash = hash_password(payload.new_password)
    current_user.must_change_password = False
    db.add(
        _password_log(
            actor=current_user,
            target=current_user,
            action="CHANGE_PASSWORD",
            request=request,
        )
    )
    db.commit()
    db.refresh(current_user)
    return {"ok": True, "user": _user_payload(current_user)}


@router.get("/users")
def list_users(
    _admin: User = Depends(RoleChecker(["admin"])),
    db: Session = Depends(get_db),
) -> dict:
    users = db.scalars(select(User).order_by(User.username)).all()
    return {"items": [_user_payload(user) for user in users]}


@router.post("/users", status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreateRequest,
    admin: User = Depends(RoleChecker(["admin"])),
    db: Session = Depends(get_db),
) -> dict:
    username = _validate_username(payload.username)
    if db.scalar(select(User.id).where(User.username == username)) is not None:
        raise HTTPException(status_code=409, detail="用户名已存在")
    _validate_new_password(payload.password)
    user = User(
        username=username,
        password_hash=hash_password(payload.password),
        role=_validate_role(payload.role),
        real_name=payload.real_name.strip() or username,
        display_name=payload.display_name.strip() if payload.display_name else None,
        is_active=payload.is_active,
        must_change_password=payload.must_change_password,
    )
    db.add(user)
    db.flush()
    db.add(
        OperationLog(
            user_id=admin.id,
            action="CREATE_USER",
            resource="User",
            details=json.dumps({"target_user_id": user.id, "username": user.username}),
            username=admin.username,
            role=admin.role,
            entity_type="user",
            entity_id=user.id,
            description="创建用户",
        )
    )
    db.commit()
    db.refresh(user)
    return {"ok": True, "user": _user_payload(user)}


@router.put("/users/{user_id}")
def update_user(
    user_id: int,
    payload: UserUpdateRequest,
    admin: User = Depends(RoleChecker(["admin"])),
    db: Session = Depends(get_db),
) -> dict:
    user = _get_user_or_404(db, user_id)
    changes = payload.model_dump(exclude_unset=True)
    removes_admin_access = (
        (changes.get("role") is not None and changes["role"] != "admin")
        or changes.get("is_active") is False
    )
    if user.id == admin.id and removes_admin_access:
        raise HTTPException(status_code=400, detail="不能停用当前管理员或修改当前管理员角色")
    if user.role == "admin" and removes_admin_access:
        active_admin_count = db.scalar(
            select(func.count(User.id)).where(User.role == "admin", User.is_active.is_(True))
        ) or 0
        if active_admin_count <= 1:
            raise HTTPException(status_code=400, detail="系统必须至少保留一个启用中的管理员")
    if "username" in changes and changes["username"] is not None:
        username = _validate_username(changes["username"])
        existing_id = db.scalar(select(User.id).where(User.username == username))
        if existing_id is not None and existing_id != user.id:
            raise HTTPException(status_code=409, detail="用户名已存在")
        user.username = username
    if "role" in changes and changes["role"] is not None:
        user.role = _validate_role(changes["role"])
    if "real_name" in changes and changes["real_name"] is not None:
        user.real_name = changes["real_name"].strip() or user.username
    if "display_name" in changes:
        user.display_name = (
            changes["display_name"].strip() if changes["display_name"] else None
        )
    if "is_active" in changes:
        user.is_active = changes["is_active"]
    if "must_change_password" in changes:
        user.must_change_password = changes["must_change_password"]
    if "password" in changes and changes["password"] is not None:
        _validate_new_password(changes["password"])
        user.password_hash = hash_password(changes["password"])
        if "must_change_password" not in changes:
            user.must_change_password = True
    db.add(
        OperationLog(
            user_id=admin.id,
            action="UPDATE_USER",
            resource="User",
            details=json.dumps({"target_user_id": user.id, "fields": sorted(changes)}),
            username=admin.username,
            role=admin.role,
            entity_type="user",
            entity_id=user.id,
            description="更新用户",
        )
    )
    db.commit()
    db.refresh(user)
    return {"ok": True, "user": _user_payload(user)}


@router.get("/users/{user_id}/permission-overrides")
def get_permission_overrides(
    user_id: int,
    _admin: User = Depends(RoleChecker(["admin"])),
    db: Session = Depends(get_db),
) -> dict:
    user = _get_user_or_404(db, user_id)
    overrides = db.scalars(
        select(UserPermissionOverride)
        .where(UserPermissionOverride.user_id == user.id)
        .order_by(UserPermissionOverride.permission_code)
    ).all()
    return {
        "user_id": user.id,
        "permissions": _permission_catalog_payload(user),
        "overrides": [
            {"permission_code": row.permission_code, "is_allowed": row.is_allowed}
            for row in overrides
        ],
        "effective_permissions": sorted(effective_permissions(user)),
    }


@router.put("/users/{user_id}/permission-overrides")
def save_permission_overrides(
    user_id: int,
    payload: PermissionOverridesRequest,
    admin: User = Depends(RoleChecker(["admin"])),
    db: Session = Depends(get_db),
) -> dict:
    user = _get_user_or_404(db, user_id)
    unknown = sorted(set(payload.overrides).difference(PERMISSION_CATALOG))
    if unknown:
        raise HTTPException(status_code=400, detail=f"未知权限代码: {', '.join(unknown)}")
    if user.role != "admin" and any(
        payload.overrides.get(code) is True for code in ADMIN_ONLY_PERMISSIONS
    ):
        raise HTTPException(status_code=400, detail="仅管理员可以使用系统备份或用户权限管理权限")
    db.execute(delete(UserPermissionOverride).where(UserPermissionOverride.user_id == user.id))
    if user.role != "admin":
        db.add_all(
            [
                UserPermissionOverride(
                    user_id=user.id,
                    permission_code=permission_code,
                    is_allowed=is_allowed,
                    granted_by=admin.id,
                )
                for permission_code, is_allowed in sorted(payload.overrides.items())
            ]
        )
    db.commit()
    db.refresh(user)
    return get_permission_overrides(user.id, admin, db)


@router.get("/users/{user_id}/customer-scopes")
def get_customer_scopes(
    user_id: int,
    _admin: User = Depends(RoleChecker(["admin"])),
    db: Session = Depends(get_db),
) -> dict:
    user = _get_user_or_404(db, user_id)
    return {
        "user_id": user.id,
        "customer_access_mode": "all" if user.role in {"admin", "boss"} else user.customer_access_mode,
        "customer_scope": sorted(customer_scope_ids(user, db)),
        "unrestricted_customer_access": has_unrestricted_customer_access(user, db),
    }


@router.put("/users/{user_id}/customer-scopes")
def save_customer_scopes(
    user_id: int,
    payload: CustomerScopesRequest,
    admin: User = Depends(RoleChecker(["admin"])),
    db: Session = Depends(get_db),
) -> dict:
    user = _get_user_or_404(db, user_id)
    if user.role in {"admin", "boss"} and payload.mode != "all":
        raise HTTPException(status_code=400, detail="管理员与老板角色固定访问全部客户")
    customer_ids = set(payload.customer_ids)
    if any(customer_id <= 0 for customer_id in customer_ids):
        raise HTTPException(status_code=400, detail="客户 ID 无效")
    existing_ids = set(
        db.scalars(select(Customer.id).where(Customer.id.in_(customer_ids))).all()
    ) if customer_ids else set()
    missing_ids = sorted(customer_ids.difference(existing_ids))
    if missing_ids:
        raise HTTPException(status_code=404, detail=f"客户不存在: {', '.join(map(str, missing_ids))}")
    db.execute(delete(UserCustomerScope).where(UserCustomerScope.user_id == user.id))
    user.customer_access_mode = "all" if user.role in {"admin", "boss"} else payload.mode
    if user.customer_access_mode == "selected":
        db.add_all(
            [
                UserCustomerScope(
                    user_id=user.id,
                    customer_id=customer_id,
                    assigned_by=admin.id,
                )
                for customer_id in sorted(customer_ids)
            ]
        )
    db.commit()
    return get_customer_scopes(user.id, admin, db)


@router.put("/users/{user_id}/access")
def save_user_access(
    user_id: int,
    payload: UserAccessRequest,
    admin: User = Depends(RoleChecker(["admin"])),
    db: Session = Depends(get_db),
) -> dict:
    """Atomically replace one user's permission overrides and customer scope."""
    user = _get_user_or_404(db, user_id)
    unknown = sorted(set(payload.overrides).difference(PERMISSION_CATALOG))
    if unknown:
        raise HTTPException(status_code=400, detail=f"未知权限代码: {', '.join(unknown)}")
    if user.role != "admin" and any(
        payload.overrides.get(code) is True for code in ADMIN_ONLY_PERMISSIONS
    ):
        raise HTTPException(status_code=400, detail="仅管理员可以使用系统备份或用户权限管理权限")
    if user.role in {"admin", "boss"} and payload.mode != "all":
        raise HTTPException(status_code=400, detail="管理员与老板角色固定访问全部客户")

    customer_ids = set(payload.customer_ids)
    if any(customer_id <= 0 for customer_id in customer_ids):
        raise HTTPException(status_code=400, detail="客户 ID 无效")
    existing_ids = (
        set(db.scalars(select(Customer.id).where(Customer.id.in_(customer_ids))).all())
        if customer_ids
        else set()
    )
    missing_ids = sorted(customer_ids.difference(existing_ids))
    if missing_ids:
        raise HTTPException(status_code=404, detail=f"客户不存在: {', '.join(map(str, missing_ids))}")

    db.execute(delete(UserPermissionOverride).where(UserPermissionOverride.user_id == user.id))
    if user.role != "admin":
        db.add_all(
            [
                UserPermissionOverride(
                    user_id=user.id,
                    permission_code=permission_code,
                    is_allowed=is_allowed,
                    granted_by=admin.id,
                )
                for permission_code, is_allowed in sorted(payload.overrides.items())
            ]
        )
    db.execute(delete(UserCustomerScope).where(UserCustomerScope.user_id == user.id))
    user.customer_access_mode = "all" if user.role in {"admin", "boss"} else payload.mode
    if user.customer_access_mode == "selected":
        db.add_all(
            [
                UserCustomerScope(
                    user_id=user.id,
                    customer_id=customer_id,
                    assigned_by=admin.id,
                )
                for customer_id in sorted(customer_ids)
            ]
        )
    db.add(
        OperationLog(
            user_id=admin.id,
            action="UPDATE_USER_ACCESS",
            resource="User",
            details=json.dumps(
                {
                    "target_user_id": user.id,
                    "permission_count": len(payload.overrides),
                    "customer_access_mode": user.customer_access_mode,
                    "customer_scope_count": len(customer_ids),
                },
                ensure_ascii=False,
            ),
            username=admin.username,
            role=admin.role,
            entity_type="user",
            entity_id=user.id,
            description="更新用户权限与客户范围",
        )
    )
    db.commit()
    db.refresh(user)
    return {
        "ok": True,
        "permission_access": get_permission_overrides(user.id, admin, db),
        "customer_access": get_customer_scopes(user.id, admin, db),
    }


@router.put("/users/{username}/reset-password")
def reset_password(
    username: str,
    payload: ResetPasswordRequest,
    request: Request,
    admin: User = Depends(RoleChecker(["admin"])),
    db: Session = Depends(get_db),
) -> dict:
    target = db.scalar(select(User).where(User.username == username.strip()))
    if target is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    _validate_new_password(payload.new_password)

    target.password_hash = hash_password(payload.new_password)
    target.must_change_password = True
    db.add(
        _password_log(
            actor=admin,
            target=target,
            action="RESET_PASSWORD",
            request=request,
        )
    )
    db.commit()
    db.refresh(target)
    return {"ok": True, "user": _user_payload(target)}
