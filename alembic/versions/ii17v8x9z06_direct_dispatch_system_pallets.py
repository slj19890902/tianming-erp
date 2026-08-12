"""allow distinct direct-dispatch pallets in the formal staging area

Revision ID: ii17v8x9z06
Revises: hh16v8x9z05
Create Date: 2026-08-12
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "ii17v8x9z06"
down_revision = "hh16v8x9z05"
branch_labels = None
depends_on = None


DEFAULT_OCCUPANCY_KEY = "PRIMARY"


def upgrade() -> None:
    op.add_column(
        "inventory_pallets",
        sa.Column(
            "location_occupancy_key",
            sa.String(length=100),
            nullable=False,
            server_default=DEFAULT_OCCUPANCY_KEY,
        ),
    )
    op.drop_index(
        "uq_inventory_pallets_current_location",
        table_name="inventory_pallets",
    )
    op.create_index(
        "uq_inventory_pallets_current_location",
        "inventory_pallets",
        ["location_id", "location_occupancy_key"],
        unique=True,
        sqlite_where=sa.text("is_current = 1"),
        postgresql_where=sa.text("is_current = true"),
    )


def downgrade() -> None:
    connection = op.get_bind()
    system_pallet_facts = int(
        connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM inventory_pallets "
                "WHERE location_occupancy_key <> :default_key"
            ),
            {"default_key": DEFAULT_OCCUPANCY_KEY},
        ).scalar_one()
        or 0
    )
    if system_pallet_facts:
        raise RuntimeError(
            "P1-49A 已存在直接待送系统栈板事实，拒绝破坏性降级；"
            "请恢复升级前完整备份。"
        )

    op.drop_index(
        "uq_inventory_pallets_current_location",
        table_name="inventory_pallets",
    )
    # Both supported engines can drop the column in place.  Avoid Alembic's
    # SQLite table-recreate path because warehouse guard triggers reference
    # inventory_pallets throughout the operation.
    op.drop_column("inventory_pallets", "location_occupancy_key")
    op.create_index(
        "uq_inventory_pallets_current_location",
        "inventory_pallets",
        ["location_id"],
        unique=True,
        sqlite_where=sa.text("is_current = 1"),
        postgresql_where=sa.text("is_current = true"),
    )
