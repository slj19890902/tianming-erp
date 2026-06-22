from __future__ import annotations

import os
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import load_settings
from app.core.database import create_engine_from_settings
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
    initial_password: str,
) -> int:
    with session_factory() as session:
        existing_users = {
            user.username: user for user in session.scalars(select(User)).all()
        }
        changed = 0
        for username, role, real_name in DEFAULT_ACCOUNTS:
            user = existing_users.get(username)
            if user is None:
                session.add(
                    User(
                        username=username,
                        password_hash=hash_password(initial_password),
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
            if needs_password_upgrade:
                user.password_hash = hash_password(initial_password)
                user.must_change_password = True
            if user.role != role:
                user.role = role
            if not user.real_name:
                user.real_name = real_name
            if not user.display_name:
                user.display_name = real_name
            if not user.is_active:
                user.is_active = True
            if needs_password_upgrade:
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
    initial_password = os.getenv("ERP_INITIAL_PASSWORD", "ChangeMe123!")
    created = initialize_users(
        session_factory,
        initial_password=initial_password,
    )
    if created:
        print(f"已创建 {created} 个测试账号，首次登录必须修改密码。")
        print("账号：admin / finance / sales / workshop")
        print("初始密码来自 ERP_INITIAL_PASSWORD；未设置时为 ChangeMe123!")
    else:
        print("users 表已有账号，未重复初始化。")


if __name__ == "__main__":
    main()
