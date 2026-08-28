from __future__ import annotations

import json
from datetime import timedelta
from threading import Lock
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

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
from app.core.password_policy import normalize_username, password_policy_issues
from app.core.security import create_session_token, hash_password, verify_password
from app.core.time_contract import utc_now_naive
from app.models.access_control import UserCustomerScope, UserPermissionOverride
from app.models.audit import OperationLog
from app.models.customer import Customer
from app.models.user import USER_ROLES, User
from app.services.audit_log import append_audit_event


router = APIRouter()


# OperationLog is the shared persistence layer for this lightweight guard.  A
# short process-local gate serializes only committed audit count/write steps;
# bcrypt always runs outside the gate.
LOGIN_FAILURE_LIMIT = 5
LOGIN_IP_FAILURE_LIMIT = 30
LOGIN_FAILURE_WINDOW = timedelta(minutes=15)
_LOGIN_AUDIT_GATE = Lock()


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
    "contracts.view": ("contracts", "客户合同查看"),
    "contracts.edit": ("contracts", "客户合同编辑"),
    "contracts.convert": ("contracts", "客户合同转订单"),
    "orders.view": ("orders", "订单查看"),
    "orders.create": ("orders", "订单新增"),
    "orders.edit": ("orders", "订单编辑"),
    "orders.status": ("orders", "订单状态操作"),
    "orders.delete": ("orders", "订单删除"),
    "orders.rollback": ("orders", "订单回滚"),
    "production.printing.view": ("production", "手机印刷工位查看"),
    "production.die_cut.view": ("production", "手机模切工位查看"),
    "requisition.view": ("requisition", "报料查看"),
    "requisition.execute": ("requisition", "报料执行"),
    "requisition.purchase_price.confirm": ("requisition", "确认正式采购价格"),
    "requisition.purchase_price.correct": ("requisition", "更正正式采购价格"),
    "dashboard.view": ("dashboard", "首页仪表盘查看"),
    "incoming.view": ("incoming", "来料入库查看"),
    "incoming.execute": ("incoming", "来料入库操作"),
    "incoming.material_variance.confirm": ("incoming", "独立确认实际材质差异"),
    "warehouse.view": ("warehouse", "仓库库存查看"),
    "warehouse.execute": ("warehouse", "仓库库存操作"),
    "warehouse.archive": ("warehouse", "模具封存与恢复"),
    "warehouse.correct": ("warehouse", "仓库位置登记纠正"),
    "warehouse.reserve": ("warehouse", "订单库存预占"),
    "warehouse.stocktake.view": ("warehouse", "库存盘点查看"),
    "warehouse.stocktake.submit": ("warehouse", "库存盘点提交"),
    "warehouse.stocktake.review": ("warehouse", "库存盘点审核"),
    "deliveries.view": ("deliveries", "送货查看"),
    "deliveries.execute": ("deliveries", "送货操作"),
    "finance.view": ("finance", "财务查看"),
    "finance.execute": ("finance", "财务操作"),
    "finance.return_receipt.period.adjust": ("finance", "回单对账归属月份调整"),
    "finance.customer_charge.manage": ("finance", "维护客户附加收费"),
    "finance.customer_charge.confirm": ("finance", "确认客户附加收费归属月份"),
    "finance.statement.confirm": ("finance", "确认月结对账单"),
    "finance.invoice_task.generate": ("finance", "生成并导出开票任务"),
    "finance.invoice_result.register": ("finance", "登记开票结果"),
    "finance.invoice_profile.manage": ("finance", "维护开票资料"),
    "finance.invoice_attachment.view": ("finance", "查看发票附件"),
    "finance.invoice_attachment.manage": ("finance", "管理发票附件"),
    "cost.view": ("sensitive", "成本/毛利查看"),
    "system.backup": ("system", "系统备份"),
    "users.manage": ("system", "用户权限管理"),
    "audit.view": ("system", "审计日志查看"),
}


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=1, max_length=72)
    remember_me: bool = False

    @field_validator("username", mode="before")
    @classmethod
    def normalize_username_input(cls, value: object) -> object:
        if isinstance(value, str):
            return normalize_username(value)
        return value

    @field_validator("password")
    @classmethod
    def validate_password_bytes(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 72:
            raise ValueError("密码 UTF-8 编码后不能超过 72 字节")
        return value


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class UiModeRequest(BaseModel):
    ui_mode: Literal["standard", "large"]


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
    ui_mode: Literal["standard", "large"]


def _user_payload(user: User) -> dict:
    return UserResponse.model_validate(user).model_dump()


def _user_admin_audit_snapshot(user: User) -> dict[str, object]:
    """Return only non-secret account facts needed for responsibility tracing."""

    return {
        "username": user.username,
        "role": user.role,
        "real_name": user.real_name,
        "display_name": user.display_name,
        "is_active": user.is_active,
        "credential_change_required": user.must_change_password,
        "customer_access_mode": user.customer_access_mode,
        "ui_mode": user.ui_mode,
        "auth_version": user.auth_version,
    }


def _auth_payload(user: User, db: Session) -> dict:
    return {
        "ok": True,
        "user": _user_payload(user),
        "session_identity": {
            "user_id": user.id,
            "auth_version": user.auth_version,
        },
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


def _permission_override_values(db: Session, user_id: int) -> list[tuple[str, bool]]:
    return [
        (row.permission_code, bool(row.is_allowed))
        for row in db.scalars(
            select(UserPermissionOverride)
            .where(UserPermissionOverride.user_id == user_id)
            .order_by(UserPermissionOverride.permission_code)
        ).all()
    ]


def _effective_permission_values(
    user: User,
    overrides: list[tuple[str, bool]],
) -> list[str]:
    if user.role == "admin":
        return sorted(PERMISSION_CATALOG)
    permissions = set(ROLE_DEFAULT_PERMISSIONS.get(user.role, frozenset()))
    for permission_code, is_allowed in overrides:
        if permission_code not in PERMISSION_CATALOG:
            continue
        if is_allowed:
            permissions.add(permission_code)
        else:
            permissions.discard(permission_code)
    permissions.difference_update(ADMIN_ONLY_PERMISSIONS)
    return sorted(permissions)


def _access_audit_snapshot_from_values(
    user: User,
    *,
    overrides: list[tuple[str, bool]],
    access_mode: str,
    customer_ids: list[int],
) -> dict:
    return {
        "permission_overrides": [
            {
                "code": permission_code,
                "decision": "allow" if is_allowed else "deny",
            }
            for permission_code, is_allowed in overrides
        ],
        "effective_permissions": _effective_permission_values(user, overrides),
        "customer_access_mode": access_mode,
        "customer_ids": sorted(customer_ids),
    }


def _current_access_values(
    db: Session,
    user: User,
) -> tuple[list[tuple[str, bool]], str, list[int]]:
    return (
        _permission_override_values(db, user.id),
        user.customer_access_mode,
        sorted(customer_scope_ids(user, db)),
    )


def _access_audit_snapshot(db: Session, user: User) -> dict:
    overrides, access_mode, customer_ids = _current_access_values(db, user)
    return _access_audit_snapshot_from_values(
        user,
        overrides=overrides,
        access_mode=access_mode,
        customer_ids=customer_ids,
    )


def _desired_permission_override_values(
    user: User,
    overrides: dict[str, bool],
) -> list[tuple[str, bool]]:
    if user.role == "admin":
        return []
    return sorted(
        (permission_code, bool(is_allowed))
        for permission_code, is_allowed in overrides.items()
    )


def _desired_customer_scope_values(
    user: User,
    *,
    mode: str,
    customer_ids: set[int],
) -> tuple[str, list[int]]:
    desired_mode = "all" if user.role in {"admin", "boss"} else mode
    desired_ids = sorted(customer_ids) if desired_mode == "selected" else []
    return desired_mode, desired_ids


def _replace_permission_overrides(
    db: Session,
    *,
    user: User,
    overrides: dict[str, bool],
    actor_id: int,
) -> None:
    desired = _desired_permission_override_values(user, overrides)
    if _permission_override_values(db, user.id) == desired:
        return
    db.execute(
        delete(UserPermissionOverride).where(UserPermissionOverride.user_id == user.id)
    )
    db.add_all(
        [
            UserPermissionOverride(
                user_id=user.id,
                permission_code=permission_code,
                is_allowed=is_allowed,
                granted_by=actor_id,
            )
            for permission_code, is_allowed in desired
        ]
    )


def _replace_customer_scopes(
    db: Session,
    *,
    user: User,
    mode: str,
    customer_ids: set[int],
    actor_id: int,
) -> None:
    desired_mode, desired_ids = _desired_customer_scope_values(
        user,
        mode=mode,
        customer_ids=customer_ids,
    )
    current_ids = sorted(customer_scope_ids(user, db))
    if user.customer_access_mode == desired_mode and current_ids == desired_ids:
        return
    db.execute(delete(UserCustomerScope).where(UserCustomerScope.user_id == user.id))
    user.customer_access_mode = desired_mode
    db.add_all(
        [
            UserCustomerScope(
                user_id=user.id,
                customer_id=customer_id,
                assigned_by=actor_id,
            )
            for customer_id in desired_ids
        ]
    )


def _claim_access_auth_version(
    db: Session,
    *,
    target: User,
    expected_auth_version: int,
    increment: bool,
) -> None:
    claimed_auth_version = expected_auth_version + 1 if increment else expected_auth_version
    try:
        result = db.execute(
            update(User)
            .where(
                User.id == target.id,
                User.auth_version == expected_auth_version,
            )
            .values(auth_version=claimed_auth_version)
            .execution_options(synchronize_session=False)
        )
    except OperationalError as error:
        if "locked" in str(error).lower():
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail="用户访问配置已被其他管理员修改，请刷新后重试",
            ) from error
        raise
    if result.rowcount != 1:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="用户访问配置已被其他管理员修改，请刷新后重试",
        )
    set_committed_value(target, "auth_version", claimed_auth_version)


def _commit_access_audit(
    db: Session,
    *,
    actor: User,
    target: User,
    action: str,
    description: str,
    before: dict,
    expected_after: dict,
    before_auth_version: int,
    changed: bool,
    request: Request,
) -> None:
    try:
        db.flush()
        after = _access_audit_snapshot(db, target)
        if after != expected_after:
            raise RuntimeError("access configuration audit snapshot mismatch")
        append_audit_event(
            db,
            request=request,
            actor=actor,
            event_category="security",
            result="success" if changed else "no_change",
            source="web",
            module_code="users",
            action_code=action,
            resource="UserAccess",
            entity_type="user",
            entity_id=target.id,
            object_ref=target.username,
            description=description,
            details={
                "action": action,
                "actor": {
                    "user_id": actor.id,
                    "username": actor.username,
                    "role": actor.role,
                },
                "target_user_id": target.id,
                "target_username": target.username,
                "result": "changed" if changed else "no_change",
                "before": before,
                "after": after,
                "auth_version": {
                    "before": before_auth_version,
                    "after": target.auth_version,
                },
            },
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(target)


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
    try:
        return normalize_username(username)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


def _validate_new_password(password: str, *, username: str | None = None) -> None:
    issues = password_policy_issues(password, username=username)
    if issues:
        raise HTTPException(status_code=400, detail=f"密码不符合要求：{'；'.join(issues)}")


def _password_log(
    db: Session,
    *,
    actor: User,
    target: User,
    action: str,
    request: Request,
) -> OperationLog:
    return append_audit_event(
        db,
        request=request,
        actor=actor,
        event_category="security",
        result="success",
        source="web",
        module_code="auth",
        action_code=action,
        resource="User",
        entity_type="user",
        entity_id=target.id,
        object_ref=target.username,
        description="修改密码" if action == "CHANGE_PASSWORD" else "管理员重置密码",
        details={"username": target.username, "target_user_id": target.id},
    )


def _login_request_metadata(request: Request, username: str) -> dict[str, str | None]:
    ip_address = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")
    if user_agent is not None:
        user_agent = user_agent[:256]
    return {
        "attempted_username": username,
        "ip_address": ip_address,
        "user_agent": user_agent,
    }


def _login_attempt_log(
    db: Session,
    *,
    action: str,
    request: Request,
    username: str,
) -> OperationLog:
    metadata = _login_request_metadata(request, username)
    return append_audit_event(
        db,
        request=request,
        event_category="security",
        result="denied" if action == "LOGIN_THROTTLED" else "failed",
        source="web",
        module_code="auth",
        action_code=action,
        resource="User",
        entity_type="user",
        object_ref=username,
        operator_name=username,
        description=(
            "\u767b\u5f55\u5931\u8d25" if action == "LOGIN_FAILED" else "\u767b\u5f55\u8bf7\u6c42\u88ab\u9650\u6d41"
        ),
        details=metadata,
    )


def _recent_failed_login_count(db: Session, *, username: str, ip_address: str | None) -> int:
    cutoff = utc_now_naive() - LOGIN_FAILURE_WINDOW
    last_success_id = db.scalar(
        select(func.max(OperationLog.id)).where(
            OperationLog.action == "LOGIN",
            OperationLog.username == username,
            OperationLog.ip_address == ip_address,
        )
    )
    filters = [
        OperationLog.action == "LOGIN_FAILED",
        OperationLog.username == username,
        OperationLog.ip_address == ip_address,
        OperationLog.created_at >= cutoff,
    ]
    if last_success_id is not None:
        filters.append(OperationLog.id > last_success_id)
    return db.scalar(select(func.count(OperationLog.id)).where(*filters)) or 0


def _recent_ip_failed_login_count(db: Session, *, ip_address: str | None) -> int:
    if ip_address is None:
        return 0
    cutoff = utc_now_naive() - LOGIN_FAILURE_WINDOW
    return (
        db.scalar(
            select(func.count(OperationLog.id)).where(
                OperationLog.action == "LOGIN_FAILED",
                OperationLog.ip_address == ip_address,
                OperationLog.created_at >= cutoff,
            )
        )
        or 0
    )


def _throttle_log_exists(
    db: Session,
    *,
    username: str,
    ip_address: str | None,
    ip_throttled: bool,
) -> bool:
    cutoff = utc_now_naive() - LOGIN_FAILURE_WINDOW
    filters = [
        OperationLog.action == "LOGIN_THROTTLED",
        OperationLog.ip_address == ip_address,
        OperationLog.created_at >= cutoff,
    ]
    if not ip_throttled:
        filters.append(OperationLog.username == username)
    return db.scalar(select(OperationLog.id).where(*filters).limit(1)) is not None


def _login_throttle_state(
    db: Session,
    *,
    username: str,
    ip_address: str | None,
) -> tuple[bool, bool]:
    username_throttled = (
        _recent_failed_login_count(
            db,
            username=username,
            ip_address=ip_address,
        )
        >= LOGIN_FAILURE_LIMIT
    )
    ip_throttled = (
        ip_address is not None
        and _recent_ip_failed_login_count(db, ip_address=ip_address)
        >= LOGIN_IP_FAILURE_LIMIT
    )
    return username_throttled, ip_throttled


def _raise_login_throttled(
    db: Session,
    *,
    request: Request,
    username: str,
    ip_address: str | None,
    ip_throttled: bool,
) -> None:
    if not _throttle_log_exists(
        db,
        username=username,
        ip_address=ip_address,
        ip_throttled=ip_throttled,
    ):
        _login_attempt_log(
            db,
            action="LOGIN_THROTTLED",
            request=request,
            username=username,
        )
    db.commit()
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="\u767b\u5f55\u5c1d\u8bd5\u8fc7\u591a\uff0c\u8bf7\u7a0d\u540e\u518d\u8bd5",
    )


def _reject_committed_throttle(
    db: Session,
    *,
    request: Request,
    username: str,
    ip_address: str | None,
) -> None:
    # This gate is deliberately held only around committed audit reads/writes.
    # Password verification is performed after it is released.
    with _LOGIN_AUDIT_GATE:
        username_throttled, ip_throttled = _login_throttle_state(
            db,
            username=username,
            ip_address=ip_address,
        )
        if username_throttled or ip_throttled:
            _raise_login_throttled(
                db,
                request=request,
                username=username,
                ip_address=ip_address,
                ip_throttled=ip_throttled,
            )
        db.rollback()


@router.post("/login")
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> dict:
    username = normalize_username(payload.username)
    ip_address = request.client.host if request.client else None
    _reject_committed_throttle(
        db,
        request=request,
        username=username,
        ip_address=ip_address,
    )
    user = db.scalar(select(User).where(User.username == username))
    user_snapshot = (
        {
            "id": user.id,
            "username": user.username,
            "role": user.role,
            "is_active": user.is_active,
            "password_hash": user.password_hash,
            "auth_version": user.auth_version,
        }
        if user is not None
        else None
    )
    # Release SQLite's read transaction before bcrypt.  Different login keys
    # may verify concurrently; only the short audit INSERT holds a write lock.
    db.rollback()
    password_valid = (
        user_snapshot is not None
        and user_snapshot["is_active"]
        and verify_password(payload.password, user_snapshot["password_hash"])
    )
    if not password_valid:
        # Recheck and record under one short process-local gate.  Concurrent
        # failures therefore cannot all pass a stale count snapshot.
        with _LOGIN_AUDIT_GATE:
            username_throttled, ip_throttled = _login_throttle_state(
                db,
                username=username,
                ip_address=ip_address,
            )
            if username_throttled or ip_throttled:
                _raise_login_throttled(
                    db,
                    request=request,
                    username=username,
                    ip_address=ip_address,
                    ip_throttled=ip_throttled,
                )
            _login_attempt_log(
                db,
                action="LOGIN_FAILED",
                request=request,
                username=username,
            )
            db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
        )

    current = getattr(request.app.state, "erp_settings", None) or load_settings()
    remember_seconds = 30 * 24 * 60 * 60
    token = create_session_token(
        user_snapshot["id"],
        auth_version=user_snapshot["auth_version"],
        secret_key=current.secret_key,
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
    # SQLite accepts one writer at a time.  Serialize only this short audit
    # commit; all password checks above remain fully concurrent.
    with _LOGIN_AUDIT_GATE:
        login_actor = db.get(User, user_snapshot["id"])
        if login_actor is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="用户名或密码错误",
            )
        append_audit_event(
            db,
            request=request,
            actor=login_actor,
            event_category="security",
            result="success",
            source="web",
            module_code="auth",
            action_code="LOGIN",
            resource="User",
            entity_type="user",
            entity_id=login_actor.id,
            object_ref=login_actor.username,
            description="用户登录",
            details={
                "username": login_actor.username,
                "role": login_actor.role,
            },
        )
        db.commit()
    user = db.get(User, user_snapshot["id"])
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
        )
    return _auth_payload(user, db)


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    # The intentionally coarse-grained first implementation revokes every
    # browser session for this user, including the one that made this request.
    current_user.auth_version += 1
    append_audit_event(
        db,
        request=request,
        actor=current_user,
        event_category="security",
        result="success",
        source="web",
        module_code="auth",
        action_code="LOGOUT",
        resource="User",
        entity_type="user",
        entity_id=current_user.id,
        object_ref=current_user.username,
        description="用户退出登录",
        details={"revoked_all_sessions": True},
    )
    db.commit()
    current = getattr(request.app.state, "erp_settings", None) or load_settings()
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


@router.put("/me/ui-mode")
def change_ui_mode(
    payload: UiModeRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    previous_ui_mode = current_user.ui_mode
    current_user.ui_mode = payload.ui_mode
    db.add(
        OperationLog(
            user_id=current_user.id,
            action="CHANGE_UI_MODE",
            resource="User",
            details=json.dumps(
                {
                    "target_user_id": current_user.id,
                    "old_ui_mode": previous_ui_mode,
                    "new_ui_mode": payload.ui_mode,
                },
                ensure_ascii=False,
            ),
            ip_address=request.client.host if request.client else None,
            username=current_user.username,
            role=current_user.role,
            entity_type="user",
            entity_id=current_user.id,
            description="切换界面模式",
            user_agent=request.headers.get("user-agent"),
        )
    )
    db.commit()
    db.refresh(current_user)
    return {"ok": True, "user": _user_payload(current_user)}


@router.put("/password")
def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    if not verify_password(payload.current_password, current_user.password_hash):
        raise HTTPException(status_code=400, detail="当前密码错误")
    _validate_new_password(payload.new_password, username=current_user.username)
    if verify_password(payload.new_password, current_user.password_hash):
        raise HTTPException(status_code=400, detail="新密码不能与当前密码相同")

    current_user.password_hash = hash_password(payload.new_password)
    current_user.must_change_password = False
    current_user.auth_version += 1
    _password_log(
        db,
        actor=current_user,
        target=current_user,
        action="CHANGE_PASSWORD",
        request=request,
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
    request: Request,
    admin: User = Depends(RoleChecker(["admin"])),
    db: Session = Depends(get_db),
) -> dict:
    username = _validate_username(payload.username)
    if db.scalar(select(User.id).where(User.username == username)) is not None:
        raise HTTPException(status_code=409, detail="用户名已存在")
    _validate_new_password(payload.password, username=username)
    role = _validate_role(payload.role)
    user = User(
        username=username,
        password_hash=hash_password(payload.password),
        role=role,
        real_name=payload.real_name.strip() or username,
        display_name=payload.display_name.strip() if payload.display_name else None,
        is_active=payload.is_active,
        must_change_password=payload.must_change_password,
        ui_mode="large" if role == "boss" else "standard",
    )
    db.add(user)
    db.flush()
    append_audit_event(
        db,
        request=request,
        actor=admin,
        event_category="security",
        result="success",
        source="web",
        module_code="users",
        action_code="CREATE_USER",
        resource="User",
        entity_type="user",
        entity_id=user.id,
        object_ref=user.username,
        description="创建用户",
        details={
            "target_user_id": user.id,
            "after": _user_admin_audit_snapshot(user),
        },
    )
    db.commit()
    db.refresh(user)
    return {"ok": True, "user": _user_payload(user)}


@router.put("/users/{user_id}")
def update_user(
    user_id: int,
    payload: UserUpdateRequest,
    request: Request,
    admin: User = Depends(RoleChecker(["admin"])),
    db: Session = Depends(get_db),
) -> dict:
    user = _get_user_or_404(db, user_id)
    before = _user_admin_audit_snapshot(user)
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
        new_role = _validate_role(changes["role"])
        if user.role != "boss" and new_role == "boss":
            user.ui_mode = "large"
        user.role = new_role
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
        _validate_new_password(changes["password"], username=user.username)
        user.password_hash = hash_password(changes["password"])
        if "must_change_password" not in changes:
            user.must_change_password = True
    if {"role", "is_active", "password"}.intersection(changes):
        user.auth_version += 1
    after = _user_admin_audit_snapshot(user)
    changed_fields = sorted(
        field_name
        for field_name in before
        if before[field_name] != after[field_name]
    )
    password_changed = (
        "password" in changes and changes["password"] is not None
    )
    append_audit_event(
        db,
        request=request,
        actor=admin,
        event_category="security",
        result=(
            "success"
            if changed_fields or password_changed
            else "no_change"
        ),
        source="web",
        module_code="users",
        action_code="UPDATE_USER",
        resource="User",
        entity_type="user",
        entity_id=user.id,
        object_ref=user.username,
        description="更新用户",
        details={
            "target_user_id": user.id,
            "changed_fields": changed_fields,
            "credential_changed": password_changed,
            "before": before,
            "after": after,
        },
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
    request: Request,
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
    current_overrides, current_mode, current_ids = _current_access_values(db, user)
    desired_overrides = _desired_permission_override_values(user, payload.overrides)
    before = _access_audit_snapshot_from_values(
        user,
        overrides=current_overrides,
        access_mode=current_mode,
        customer_ids=current_ids,
    )
    expected_after = _access_audit_snapshot_from_values(
        user,
        overrides=desired_overrides,
        access_mode=current_mode,
        customer_ids=current_ids,
    )
    changed = before != expected_after
    before_auth_version = user.auth_version
    try:
        _claim_access_auth_version(
            db,
            target=user,
            expected_auth_version=before_auth_version,
            increment=changed,
        )
        _replace_permission_overrides(
            db,
            user=user,
            overrides=payload.overrides,
            actor_id=admin.id,
        )
        _commit_access_audit(
            db,
            actor=admin,
            target=user,
            action="UPDATE_PERMISSION_OVERRIDES",
            description="更新用户权限覆盖",
            before=before,
            expected_after=expected_after,
            before_auth_version=before_auth_version,
            changed=changed,
            request=request,
        )
    except Exception:
        db.rollback()
        raise
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
    request: Request,
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
    current_overrides, current_mode, current_ids = _current_access_values(db, user)
    desired_mode, desired_ids = _desired_customer_scope_values(
        user,
        mode=payload.mode,
        customer_ids=customer_ids,
    )
    before = _access_audit_snapshot_from_values(
        user,
        overrides=current_overrides,
        access_mode=current_mode,
        customer_ids=current_ids,
    )
    expected_after = _access_audit_snapshot_from_values(
        user,
        overrides=current_overrides,
        access_mode=desired_mode,
        customer_ids=desired_ids,
    )
    changed = before != expected_after
    before_auth_version = user.auth_version
    try:
        _claim_access_auth_version(
            db,
            target=user,
            expected_auth_version=before_auth_version,
            increment=changed,
        )
        _replace_customer_scopes(
            db,
            user=user,
            mode=payload.mode,
            customer_ids=customer_ids,
            actor_id=admin.id,
        )
        _commit_access_audit(
            db,
            actor=admin,
            target=user,
            action="UPDATE_CUSTOMER_SCOPES",
            description="更新用户客户范围",
            before=before,
            expected_after=expected_after,
            before_auth_version=before_auth_version,
            changed=changed,
            request=request,
        )
    except Exception:
        db.rollback()
        raise
    return get_customer_scopes(user.id, admin, db)


@router.put("/users/{user_id}/access")
def save_user_access(
    user_id: int,
    payload: UserAccessRequest,
    request: Request,
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

    current_overrides, current_mode, current_ids = _current_access_values(db, user)
    desired_overrides = _desired_permission_override_values(user, payload.overrides)
    desired_mode, desired_ids = _desired_customer_scope_values(
        user,
        mode=payload.mode,
        customer_ids=customer_ids,
    )
    before = _access_audit_snapshot_from_values(
        user,
        overrides=current_overrides,
        access_mode=current_mode,
        customer_ids=current_ids,
    )
    expected_after = _access_audit_snapshot_from_values(
        user,
        overrides=desired_overrides,
        access_mode=desired_mode,
        customer_ids=desired_ids,
    )
    changed = before != expected_after
    before_auth_version = user.auth_version
    try:
        _claim_access_auth_version(
            db,
            target=user,
            expected_auth_version=before_auth_version,
            increment=changed,
        )
        _replace_permission_overrides(
            db,
            user=user,
            overrides=payload.overrides,
            actor_id=admin.id,
        )
        _replace_customer_scopes(
            db,
            user=user,
            mode=payload.mode,
            customer_ids=customer_ids,
            actor_id=admin.id,
        )
        _commit_access_audit(
            db,
            actor=admin,
            target=user,
            action="UPDATE_USER_ACCESS",
            description="更新用户权限与客户范围",
            before=before,
            expected_after=expected_after,
            before_auth_version=before_auth_version,
            changed=changed,
            request=request,
        )
    except Exception:
        db.rollback()
        raise
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
    normalized_username = _validate_username(username)
    target = db.scalar(select(User).where(User.username == normalized_username))
    if target is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    _validate_new_password(payload.new_password, username=target.username)

    target.password_hash = hash_password(payload.new_password)
    target.must_change_password = True
    target.auth_version += 1
    _password_log(
        db,
        actor=admin,
        target=target,
        action="RESET_PASSWORD",
        request=request,
    )
    db.commit()
    db.refresh(target)
    return {"ok": True, "user": _user_payload(target)}
