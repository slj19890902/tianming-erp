from __future__ import annotations

from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from app.core.config import load_settings


MAX_BCRYPT_PASSWORD_BYTES = 72


def _password_bytes(password: str) -> bytes:
    if not password:
        raise ValueError("密码不能为空")
    encoded = password.encode("utf-8")
    if len(encoded) > MAX_BCRYPT_PASSWORD_BYTES:
        raise ValueError("密码 UTF-8 编码后不能超过 72 字节")
    return encoded


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_password_bytes(password), bcrypt.gensalt(rounds=12)).decode(
        "ascii"
    )


def is_bcrypt_hash(password_hash: str) -> bool:
    return password_hash.startswith(("$2a$", "$2b$", "$2y$"))


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(
            _password_bytes(password),
            password_hash.encode("ascii"),
        )
    except (TypeError, ValueError):
        return False


def create_session_token(
    user_id: int,
    *,
    secret_key: str | None = None,
    expires_minutes: int | None = None,
) -> str:
    current = load_settings()
    now = datetime.now(timezone.utc)
    expires = now + timedelta(
        minutes=expires_minutes or current.session_expire_minutes
    )
    return jwt.encode(
        {
            "sub": str(user_id),
            "iat": now,
            "exp": expires,
            "type": "session",
        },
        secret_key or current.secret_key,
        algorithm="HS256",
    )


def decode_session_token(
    token: str,
    *,
    secret_key: str | None = None,
) -> int:
    current = load_settings()
    try:
        payload = jwt.decode(
            token,
            secret_key or current.secret_key,
            algorithms=["HS256"],
        )
        if payload.get("type") != "session":
            raise ValueError
        return int(payload["sub"])
    except (jwt.PyJWTError, KeyError, TypeError, ValueError) as error:
        raise ValueError("登录凭证无效或已过期") from error


def create_tianhua_pick_token(
    batch_id: int,
    draft_id: int,
    *,
    secret_key: str | None = None,
    expires_hours: int = 24,
) -> tuple[str, datetime]:
    current = load_settings()
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=expires_hours)
    token = jwt.encode(
        {
            "batch_id": batch_id,
            "draft_id": draft_id,
            "iat": now,
            "exp": expires,
            "type": "tianhua_mobile_pick",
        },
        secret_key or current.secret_key,
        algorithm="HS256",
    )
    return token, expires


def decode_tianhua_pick_token(
    token: str,
    *,
    secret_key: str | None = None,
) -> tuple[int, int]:
    current = load_settings()
    try:
        payload = jwt.decode(
            token,
            secret_key or current.secret_key,
            algorithms=["HS256"],
        )
        if payload.get("type") != "tianhua_mobile_pick":
            raise ValueError
        return int(payload["batch_id"]), int(payload["draft_id"])
    except jwt.ExpiredSignatureError as error:
        raise ValueError("二维码已过期，请在电脑端重新生成") from error
    except (jwt.PyJWTError, KeyError, TypeError, ValueError) as error:
        raise ValueError("无权限访问该拿货单") from error
