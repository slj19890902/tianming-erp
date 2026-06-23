"""Add auth audit and migrate legacy users safely.

Revision ID: 1a03b26f44c4
Revises: d0607640f8ea
Create Date: 2026-06-13
"""

from datetime import datetime
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "1a03b26f44c4"
down_revision: Union[str, Sequence[str], None] = "d0607640f8ea"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TARGET_USER_COLUMNS = {
    "id",
    "username",
    "password_hash",
    "role",
    "real_name",
    "display_name",
    "is_active",
    "must_change_password",
    "created_at",
    "updated_at",
}


def _datetime_value(value):
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _create_users_table(table_name: str = "users") -> sa.Table:
    return op.create_table(
        table_name,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("username", sa.String(length=50), nullable=False),
        sa.Column("password_hash", sa.String(length=300), nullable=False),
        sa.Column("role", sa.String(length=30), nullable=False),
        sa.Column("real_name", sa.String(length=100), nullable=False),
        sa.Column("display_name", sa.String(length=100), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column(
            "must_change_password",
            sa.Boolean(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "role IN ('admin', 'finance', 'sales', 'workshop')",
            name=f"ck_{table_name}_role_valid",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
    )


def _create_operation_logs_table() -> sa.Table:
    return op.create_table(
        "operation_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=30), nullable=False),
        sa.Column("resource", sa.String(length=100), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("username", sa.String(length=50), nullable=True),
        sa.Column("role", sa.String(length=30), nullable=True),
        sa.Column("entity_type", sa.String(length=100), nullable=True),
        sa.Column("entity_id", sa.Integer(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("extra_json", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def _migrate_users(bind, table_names: set[str]) -> None:
    inspector = sa.inspect(bind)
    user_columns = {column["name"] for column in inspector.get_columns("users")}
    if TARGET_USER_COLUMNS.issubset(user_columns):
        return

    legacy_users = [
        dict(row)
        for row in bind.execute(sa.text("SELECT * FROM users")).mappings()
    ]
    legacy_logs = []
    if "operation_logs" in table_names:
        legacy_logs = [
            dict(row)
            for row in bind.execute(
                sa.text("SELECT * FROM operation_logs")
            ).mappings()
        ]
        op.drop_table("operation_logs")
        table_names.remove("operation_logs")

    op.drop_table("users")
    users_table = _create_users_table()
    existing_names = {
        str(row.get("username") or "").strip() for row in legacy_users
    }
    migrated_users = []
    for row in legacy_users:
        username = str(row.get("username") or "").strip()
        if username == "boss" and "admin" not in existing_names:
            username = "admin"
        role = str(row.get("role") or "").strip()
        if role == "boss":
            role = "admin"
        if role not in {"admin", "finance", "sales", "workshop"}:
            role = "sales"
        display_name = row.get("display_name")
        migrated_users.append(
            {
                "id": row["id"],
                "username": username,
                "password_hash": row["password_hash"],
                "role": role,
                "real_name": row.get("real_name")
                or display_name
                or username,
                "display_name": display_name,
                "is_active": bool(row.get("is_active", 1)),
                "must_change_password": True,
                "created_at": _datetime_value(row.get("created_at")),
                "updated_at": _datetime_value(row.get("updated_at")),
            }
        )
    if migrated_users:
        op.bulk_insert(users_table, migrated_users)

    op.create_index("ix_users_username", "users", ["username"], unique=True)
    op.create_index("ix_users_role", "users", ["role"], unique=False)

    if legacy_logs:
        logs_table = _create_operation_logs_table()
        migrated_logs = []
        for row in legacy_logs:
            migrated_logs.append(
                {
                    "id": row["id"],
                    "user_id": row.get("user_id"),
                    "action": row.get("action") or "LEGACY",
                    "resource": row.get("resource")
                    or row.get("entity_type")
                    or "Legacy",
                    "details": row.get("details")
                    or row.get("extra_json")
                    or row.get("description"),
                    "ip_address": row.get("ip_address"),
                    "created_at": _datetime_value(row.get("created_at")),
                    "username": row.get("username"),
                    "role": (
                        "admin" if row.get("role") == "boss" else row.get("role")
                    ),
                    "entity_type": row.get("entity_type"),
                    "entity_id": row.get("entity_id"),
                    "description": row.get("description"),
                    "user_agent": row.get("user_agent"),
                    "extra_json": row.get("extra_json"),
                }
            )
        op.bulk_insert(logs_table, migrated_logs)
        table_names.add("operation_logs")


def upgrade() -> None:
    bind = op.get_bind()
    table_names = set(sa.inspect(bind).get_table_names())

    if "users" in table_names:
        _migrate_users(bind, table_names)
    else:
        _create_users_table()
        op.create_index("ix_users_username", "users", ["username"], unique=True)
        op.create_index("ix_users_role", "users", ["role"], unique=False)

    if "operation_logs" not in table_names:
        _create_operation_logs_table()

    index_names = {
        index["name"]
        for index in sa.inspect(bind).get_indexes("operation_logs")
    }
    for name, columns in (
        ("ix_operation_logs_user_id", ["user_id"]),
        ("ix_operation_logs_action", ["action"]),
        ("ix_operation_logs_resource", ["resource"]),
        ("ix_operation_logs_created_at", ["created_at"]),
    ):
        if name not in index_names:
            op.create_index(name, "operation_logs", columns, unique=False)


def downgrade() -> None:
    # Authentication and audit history are deliberately non-destructive.
    return
