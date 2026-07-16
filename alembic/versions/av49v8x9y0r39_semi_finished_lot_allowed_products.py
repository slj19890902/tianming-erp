"""Add hard product bindings for individual semi-finished inventory lots."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "av49v8x9y0r39"
down_revision = "au48v8x9y0q38"
branch_labels = None
depends_on = None


DOWNGRADE_BLOCKED_MESSAGE = (
    "半成品批次款号硬绑定已有记录，禁止降级删除历史关联"
)


def upgrade() -> None:
    op.create_table(
        "semi_finished_lot_allowed_products",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("inventory_lot_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("confirmed_by", sa.Integer(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["inventory_lot_id"], ["inventory_lots.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["product_id"], ["products.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["confirmed_by"], ["users.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint(
            "inventory_lot_id",
            "product_id",
            name="uq_semi_finished_lot_allowed_products_lot_product",
        ),
    )
    op.create_index(
        "ix_semi_finished_lot_allowed_products_product_lot",
        "semi_finished_lot_allowed_products",
        ["product_id", "inventory_lot_id"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    has_rows = connection.execute(
        sa.text("SELECT EXISTS(SELECT 1 FROM semi_finished_lot_allowed_products)")
    ).scalar_one()
    if has_rows:
        raise RuntimeError(DOWNGRADE_BLOCKED_MESSAGE)
    op.drop_index(
        "ix_semi_finished_lot_allowed_products_product_lot",
        table_name="semi_finished_lot_allowed_products",
    )
    op.drop_table("semi_finished_lot_allowed_products")
