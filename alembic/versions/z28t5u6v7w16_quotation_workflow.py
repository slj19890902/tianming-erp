"""Customer quotation workflow.

Revision ID: z28t5u6v7w16
Revises: x06r3s4t5u94
"""

from alembic import op
import sqlalchemy as sa


revision = "z28t5u6v7w16"
down_revision = "x06r3s4t5u94"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "quotation_daily_sequences",
        sa.Column("sequence_date", sa.Date(), primary_key=True),
        sa.Column("last_value", sa.Integer(), nullable=False),
    )
    op.create_table(
        "quotation_orders",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("quotation_no", sa.String(40), nullable=False),
        sa.Column(
            "customer_id",
            sa.Integer(),
            sa.ForeignKey("customers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("customer_name", sa.String(250), nullable=False),
        sa.Column("quotation_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("total_amount", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("remarks", sa.Text()),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime()),
        sa.CheckConstraint(
            "status IN ('draft','quoted','accepted','converted','voided')",
            name="ck_quotation_orders_status",
        ),
        sa.UniqueConstraint("quotation_no", name="uq_quotation_orders_no"),
    )
    op.create_index(
        "ix_quotation_orders_customer_id",
        "quotation_orders",
        ["customer_id"],
    )
    op.create_table(
        "quotation_items",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "quotation_id",
            sa.Integer(),
            sa.ForeignKey("quotation_orders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("product_name", sa.String(250), nullable=False),
        sa.Column("temporary_code", sa.String(150)),
        sa.Column("box_type", sa.String(150), nullable=False),
        sa.Column("length_mm", sa.Numeric(12, 2)),
        sa.Column("width_mm", sa.Numeric(12, 2)),
        sa.Column("height_mm", sa.Numeric(12, 2)),
        sa.Column("material_id", sa.Integer(), sa.ForeignKey("materials.id", ondelete="SET NULL")),
        sa.Column("material_supplier", sa.String(200)),
        sa.Column("material_code", sa.String(100)),
        sa.Column("flute_type", sa.String(50)),
        sa.Column("quantity", sa.Integer()),
        sa.Column("estimated_unit_cost", sa.Numeric(12, 4)),
        sa.Column("margin_rate", sa.Numeric(7, 4), nullable=False, server_default="20"),
        sa.Column("suggested_unit_price", sa.Numeric(12, 4)),
        sa.Column("final_unit_price", sa.Numeric(12, 4), nullable=False),
        sa.Column("remarks", sa.Text()),
        sa.Column(
            "converted_product_id",
            sa.Integer(),
            sa.ForeignKey("products.id", ondelete="SET NULL"),
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime()),
    )
    op.create_index(
        "ix_quotation_items_quotation_id",
        "quotation_items",
        ["quotation_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_quotation_items_quotation_id", table_name="quotation_items")
    op.drop_table("quotation_items")
    op.drop_index("ix_quotation_orders_customer_id", table_name="quotation_orders")
    op.drop_table("quotation_orders")
    op.drop_table("quotation_daily_sequences")
