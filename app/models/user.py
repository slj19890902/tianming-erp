from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models import Base


USER_ROLES = ("admin", "boss", "finance", "sales", "workshop", "delivery_picker")


def _default_ui_mode(context) -> str:
    return "large" if context.get_current_parameters().get("role") == "boss" else "standard"


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "role IN ('admin', 'boss', 'finance', 'sales', 'workshop', 'delivery_picker')",
            name="ck_users_role_valid",
        ),
        CheckConstraint(
            "customer_access_mode IN ('all', 'selected')",
            name="ck_users_customer_access_mode_valid",
        ),
        CheckConstraint(
            "ui_mode IN ('standard', 'large')",
            name="ck_users_ui_mode_valid",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(300))
    role: Mapped[str] = mapped_column(String(30), index=True)
    real_name: Mapped[str] = mapped_column(String(100))
    display_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Bump this value whenever a security-relevant account property changes.
    # Session JWTs carry the value they were issued with, so one bump revokes
    # every existing browser session for this user.
    auth_version: Mapped[int] = mapped_column(
        Integer,
        default=1,
        server_default="1",
        nullable=False,
    )
    must_change_password: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )
    customer_access_mode: Mapped[str] = mapped_column(
        String(20),
        default="all",
        server_default="all",
        nullable=False,
    )
    ui_mode: Mapped[str] = mapped_column(
        String(20),
        default=_default_ui_mode,
        server_default="standard",
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        nullable=False,
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        onupdate=func.current_timestamp(),
        nullable=True,
    )
    operation_logs: Mapped[list["OperationLog"]] = relationship(
        back_populates="user",
        passive_deletes=True,
    )
    permission_overrides: Mapped[list["UserPermissionOverride"]] = relationship(
        back_populates="user",
        foreign_keys="UserPermissionOverride.user_id",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    customer_scopes: Mapped[list["UserCustomerScope"]] = relationship(
        back_populates="user",
        foreign_keys="UserCustomerScope.user_id",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
