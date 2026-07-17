from __future__ import annotations

import unicodedata


MIN_PASSWORD_LENGTH = 12
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
    if len(password) < MIN_PASSWORD_LENGTH:
        issues.append(f"至少 {MIN_PASSWORD_LENGTH} 位")
    if lowered in WEAK_PASSWORDS:
        issues.append("不能使用弱密码")
    if username and username.strip().lower() in lowered:
        issues.append("不能包含用户名")
    if not any(character.islower() for character in password):
        issues.append("至少包含 1 个小写字母")
    if not any(character.isupper() for character in password):
        issues.append("至少包含 1 个大写字母")
    if not any(character.isdigit() for character in password):
        issues.append("至少包含 1 个数字")
    if not any(not character.isalnum() for character in password):
        issues.append("至少包含 1 个符号")
    if len(password.encode("utf-8")) > 72:
        issues.append("UTF-8 编码后不能超过 72 字节")
    return issues
