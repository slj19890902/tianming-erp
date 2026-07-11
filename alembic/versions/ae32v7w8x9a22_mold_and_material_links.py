"""Add mold master and material links for stock replenishment.

Revision ID: ae32v7w8x9a22
Revises: ad31v7w8x9z21
Create Date: 2026-07-11
"""

from alembic import op
import sqlalchemy as sa


revision = "ae32v7w8x9a22"
down_revision = "ad31v7w8x9z21"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mold_tools",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("mold_code", sa.String(100), nullable=False),
        sa.Column("mold_name", sa.String(200), nullable=False),
        sa.Column("rack_location", sa.String(250), nullable=False),
        sa.Column("remarks", sa.Text()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("updated_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime()),
        sa.UniqueConstraint("mold_code", name="uq_mold_tools_code"),
    )
    op.create_index(
        "ix_mold_tools_active_location",
        "mold_tools",
        ["is_active", "rack_location"],
    )

    with op.batch_alter_table("products") as batch_op:
        batch_op.add_column(sa.Column("mold_tool_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_products_mold_tool_id",
            "mold_tools",
            ["mold_tool_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index("ix_products_mold_tool_id", ["mold_tool_id"])

    with op.batch_alter_table("stock_replenishment_order_items") as batch_op:
        batch_op.add_column(sa.Column("material_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_stock_replenishment_items_material_id",
            "materials",
            ["material_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index("ix_stock_replenishment_items_material", ["material_id"])

    with op.batch_alter_table("semi_finished_inventory_details") as batch_op:
        batch_op.add_column(sa.Column("material_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_semi_inventory_material_id",
            "materials",
            ["material_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index("ix_semi_inventory_material_id", ["material_id"])


def downgrade() -> None:
    with op.batch_alter_table("semi_finished_inventory_details") as batch_op:
        batch_op.drop_index("ix_semi_inventory_material_id")
        batch_op.drop_constraint("fk_semi_inventory_material_id", type_="foreignkey")
        batch_op.drop_column("material_id")

    with op.batch_alter_table("stock_replenishment_order_items") as batch_op:
        batch_op.drop_index("ix_stock_replenishment_items_material")
        batch_op.drop_constraint(
            "fk_stock_replenishment_items_material_id", type_="foreignkey"
        )
        batch_op.drop_column("material_id")

    with op.batch_alter_table("products") as batch_op:
        batch_op.drop_index("ix_products_mold_tool_id")
        batch_op.drop_constraint("fk_products_mold_tool_id", type_="foreignkey")
        batch_op.drop_column("mold_tool_id")

    op.drop_index("ix_mold_tools_active_location", table_name="mold_tools")
    op.drop_table("mold_tools")
