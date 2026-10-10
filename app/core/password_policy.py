from __future__ import annotations

import unicodedata


DEFAULT_MIN_PASSWORD_LENGTH = 8
ADMIN_MIN_PASSWORD_LENGTH = 10
MAX_USERNAME_LENGTH = 50
WEAK_PASSWORDS = frozenset(
    {
        "admin",
        "123456",
        "12345678",
        "888888",
        "changeme123!",
        "password",
        "password123",
        "qwerty",
    }
)


def normalize_username(username: str) -> str:
    normalized = unicodedata.normalize("NFKC", username).strip()
    if not normalized:
        raise ValueError("用户名不能为空")
    if len(normalized) > MAX_USERNAME_LENGTH:
        raise ValueError(f"用户名长度不能超过 {MAX_USERNAME_LENGTH}")
    return normalized


def password_policy_issues(password: str, *, username: str | None = None) -> list[str]:
    issues: list[str] = []
    lowered = password.lower()
    normalized_username = (
        unicodedata.normalize("NFKC", username).strip().casefold()
        if username
        else None
    )
    minimum_length = (
        ADMIN_MIN_PASSWORD_LENGTH
        if normalized_username == "admin"
        else DEFAULT_MIN_PASSWORD_LENGTH
    )
    if len(password) < minimum_length:
        issues.append(f"至少 {minimum_length} 位")
    if lowered in WEAK_PASSWORDS:
        issues.append("不能使用弱密码")
    if normalized_username and normalized_username in lowered:
        issues.append("不能包含用户名")
    if not any(character.isalpha() for character in password):
        issues.append("至少包含 1 个字母")
    if not any(character.isdigit() for character in password):
        issues.append("至少包含 1 个数字")
    if len(password.encode("utf-8")) > 72:
        issues.append("UTF-8 编码后不能超过 72 字节")
    return issues
