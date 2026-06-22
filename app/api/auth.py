from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import RoleChecker, get_current_user, get_db
from app.core.config import load_settings
from app.core.security import create_session_token, hash_password, verify_password
from app.models.audit import OperationLog
from app.models.user import User


router = APIRouter()


class LoginRequest(BaseModel):
    username: str
    password: str
    remember_me: bool = False


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class ResetPasswordRequest(BaseModel):
    new_password: str


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    role: str
    real_name: str
    display_name: str | None
    must_change_password: bool


def _user_payload(user: User) -> dict:
    return UserResponse.model_validate(user).model_dump()


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
    return {"ok": True, "user": _user_payload(user)}


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
def me(current_user: User = Depends(get_current_user)) -> dict:
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
