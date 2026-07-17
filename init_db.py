from __future__ import annotations

import os
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import load_settings
from app.core.database import create_engine_from_settings
from app.core.password_policy import password_policy_issues
from app.core.security import hash_password, is_bcrypt_hash
from app.models import Base
from app.models.user import User


DEFAULT_ACCOUNTS = (
    ("admin", "admin", "系统管理员"),
    ("finance", "finance", "财务"),
    ("sales", "sales", "销售办公室"),
    ("workshop", "workshop", "车间仓库"),
)


def initialize_users(
    session_factory: Callable[[], Session],
    *,
    initial_password: str | None,
    environment: str = "development",
) -> int:
    with session_factory() as session:
        existing_users = {
            user.username: user for user in session.scalars(select(User)).all()
        }
        if environment == "production":
            if existing_users:
                raise RuntimeError(
                    "生产 users 表已有账号，init_db 禁止新增或修改任何默认账号；"
                    "请通过受控账号维护流程 scripts/admin/manage_users.py 处理"
                )
            if os.getenv("ERP_ALLOW_PRODUCTION_USER_BOOTSTRAP", "").strip() != "1":
                raise RuntimeError(
                    "生产首次账号引导默认关闭；仅空 users 表可显式设置 "
                    "ERP_ALLOW_PRODUCTION_USER_BOOTSTRAP=1"
                )
        requires_auth_change = any(
            (user := existing_users.get(username)) is None
            or not is_bcrypt_hash(user.password_hash)
            or user.role != role
            or not user.is_active
            for username, role, _real_name in DEFAULT_ACCOUNTS
        )
        if environment == "production" and requires_auth_change:
            if not initial_password:
                raise RuntimeError(
                    "生产环境首次账号引导必须显式设置 ERP_INITIAL_PASSWORD"
                )
            issues = password_policy_issues(initial_password)
            if issues:
                raise RuntimeError(
                    f"生产环境 ERP_INITIAL_PASSWORD 不符合密码策略：{'；'.join(issues)}"
                )
        password_for_changes = initial_password or "ChangeMe123!"
        changed = 0
        for username, role, real_name in DEFAULT_ACCOUNTS:
            user = existing_users.get(username)
            if user is None:
                session.add(
                    User(
                        username=username,
                        password_hash=hash_password(password_for_changes),
                        role=role,
                        real_name=real_name,
                        display_name=real_name,
                        is_active=True,
                        must_change_password=True,
                    )
                )
                changed += 1
                continue

            needs_password_upgrade = not is_bcrypt_hash(user.password_hash)
            auth_sensitive_change = False
            if needs_password_upgrade:
                user.password_hash = hash_password(password_for_changes)
                user.must_change_password = True
                auth_sensitive_change = True
            if user.role != role:
                user.role = role
                auth_sensitive_change = True
            if not user.real_name:
                user.real_name = real_name
            if not user.display_name:
                user.display_name = real_name
            if not user.is_active:
                user.is_active = True
                auth_sensitive_change = True
            if auth_sensitive_change:
                user.auth_version += 1
                changed += 1

        session.commit()
        return changed


def main() -> None:
    from sqlalchemy.orm import sessionmaker

    current_settings = load_settings()
    current_engine = create_engine_from_settings(current_settings)
    Base.metadata.create_all(current_engine)
    session_factory = sessionmaker(
        bind=current_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    configured_password = os.getenv("ERP_INITIAL_PASSWORD")
    initial_password = configured_password or (
        None if current_settings.is_production else "ChangeMe123!"
    )
    created = initialize_users(
        session_factory,
        initial_password=initial_password,
        environment=current_settings.environment,
    )
    if created:
        print(f"已创建 {created} 个测试账号，首次登录必须修改密码。")
        print("账号：admin / finance / sales / workshop")
        if current_settings.is_production:
            print("初始密码来自显式 ERP_INITIAL_PASSWORD。")
        else:
            print("初始密码来自 ERP_INITIAL_PASSWORD；开发/测试未设置时为 ChangeMe123!")
    else:
        print("users 表已有账号，未重复初始化。")


if __name__ == "__main__":
    main()
