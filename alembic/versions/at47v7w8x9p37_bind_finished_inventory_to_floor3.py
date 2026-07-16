"""Bind official finished-goods lots to floor-three pallet contents.

Revision ID: at47v7w8x9p37
Revises: as46v7w8x9o36
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "at47v7w8x9p37"
down_revision = "as46v7w8x9o36"
branch_labels = None
depends_on = None


DOWNGRADE_BLOCKED_MESSAGE = (
    "floor3 finished-inventory binding downgrade blocked: "
    "linked official inventory lots still exist"
)


def upgrade() -> None:
    with op.batch_alter_table("inventory_pallet_items") as batch:
        batch.add_column(sa.Column("inventory_lot_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_inventory_pallet_items_inventory_lot",
            "inventory_lots",
            ["inventory_lot_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.create_unique_constraint(
            "uq_inventory_pallet_items_inventory_lot", ["inventory_lot_id"]
        )


def downgrade() -> None:
    connection = op.get_bind()
    linked_count = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM inventory_pallet_items "
            "WHERE inventory_lot_id IS NOT NULL"
        )
    ).scalar_one()
    if int(linked_count or 0) > 0:
        raise RuntimeError(DOWNGRADE_BLOCKED_MESSAGE)
    with op.batch_alter_table("inventory_pallet_items") as batch:
        batch.drop_constraint(
            "uq_inventory_pallet_items_inventory_lot", type_="unique"
        )
        batch.drop_constraint(
            "fk_inventory_pallet_items_inventory_lot", type_="foreignkey"
        )
        batch.drop_column("inventory_lot_id")
