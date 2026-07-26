"""add customer contract workflow

Revision ID: cp72v8x9z61
Revises: co71v8x9z60
Create Date: 2026-07-26
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "cp72v8x9z61"
down_revision: Union[str, Sequence[str], None] = "co71v8x9z60"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "contract_daily_sequences",
        sa.Column("sequence_date", sa.Date(), nullable=False),
        sa.Column("last_value", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("sequence_date"),
    )
    op.create_table(
        "customer_contracts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("contract_no", sa.String(length=40), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("customer_name", sa.String(length=250), nullable=False),
        sa.Column("customer_contact", sa.String(length=100), nullable=True),
        sa.Column("customer_phone", sa.String(length=100), nullable=True),
        sa.Column("customer_address", sa.Text(), nullable=True),
        sa.Column("invoice_title", sa.String(length=200), nullable=True),
        sa.Column("tax_no", sa.String(length=100), nullable=True),
        sa.Column("bank_account", sa.String(length=200), nullable=True),
        sa.Column("payment_terms", sa.String(length=200), nullable=True),
        sa.Column("contract_date", sa.Date(), nullable=False),
        sa.Column("customer_po", sa.String(length=150), nullable=True),
        sa.Column("delivery_date", sa.Date(), nullable=True),
        sa.Column("remarks", sa.Text(), nullable=True),
        sa.Column("total_amount", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="draft"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("confirmed_by", sa.Integer(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("converted_by", sa.Integer(), nullable=True),
        sa.Column("converted_at", sa.DateTime(), nullable=True),
        sa.Column("converted_order_id", sa.Integer(), nullable=True),
        sa.Column("conversion_idempotency_key", sa.String(length=120), nullable=True),
        sa.CheckConstraint("status IN ('draft', 'confirmed', 'converted')", name="ck_customer_contracts_status"),
        sa.CheckConstraint("version >= 1", name="ck_customer_contracts_version"),
        sa.CheckConstraint("total_amount >= 0", name="ck_customer_contracts_total_amount"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["confirmed_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["converted_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["converted_order_id"], ["sales_orders.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("contract_no", name="uq_customer_contracts_contract_no"),
        sa.UniqueConstraint("converted_order_id", name="uq_customer_contracts_converted_order_id"),
        sa.UniqueConstraint("conversion_idempotency_key", name="uq_customer_contracts_conversion_idempotency_key"),
    )
    op.create_index("ix_customer_contracts_customer_id", "customer_contracts", ["customer_id"])
    op.create_index("ix_customer_contracts_status", "customer_contracts", ["status"])
    op.create_table(
        "customer_contract_items",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("contract_id", sa.Integer(), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("product_code", sa.String(length=150), nullable=True),
        sa.Column("product_name", sa.String(length=250), nullable=False),
        sa.Column("specification", sa.String(length=250), nullable=True),
        sa.Column("box_style", sa.String(length=150), nullable=True),
        sa.Column("length_mm", sa.Numeric(12, 2), nullable=True),
        sa.Column("width_mm", sa.Numeric(12, 2), nullable=True),
        sa.Column("height_mm", sa.Numeric(12, 2), nullable=True),
        sa.Column("material_code", sa.String(length=100), nullable=True),
        sa.Column("flute_type", sa.String(length=20), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price", sa.Numeric(12, 4), nullable=False),
        sa.Column("subtotal", sa.Numeric(14, 2), nullable=False),
        sa.Column("remarks", sa.Text(), nullable=True),
        sa.CheckConstraint("line_no >= 1", name="ck_customer_contract_items_line_no"),
        sa.CheckConstraint("quantity > 0", name="ck_customer_contract_items_quantity"),
        sa.CheckConstraint("unit_price >= 0", name="ck_customer_contract_items_unit_price"),
        sa.CheckConstraint("subtotal >= 0", name="ck_customer_contract_items_subtotal"),
        sa.ForeignKeyConstraint(["contract_id"], ["customer_contracts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("contract_id", "line_no", name="uq_customer_contract_items_line"),
    )
    op.create_index("ix_customer_contract_items_product_id", "customer_contract_items", ["product_id"])
    with op.batch_alter_table("sales_orders") as batch_op:
        batch_op.add_column(sa.Column("source_contract_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_sales_orders_source_contract_id",
            "customer_contracts",
            ["source_contract_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_unique_constraint(
            "uq_sales_orders_source_contract_id", ["source_contract_id"]
        )


def downgrade() -> None:
    connection = op.get_bind()
    fact_checks = (
        ("合同编号序列", "SELECT COUNT(*) FROM contract_daily_sequences"),
        ("合同明细", "SELECT COUNT(*) FROM customer_contract_items"),
        ("合同主表", "SELECT COUNT(*) FROM customer_contracts"),
        (
            "合同审计日志",
            """
            SELECT COUNT(*)
            FROM operation_logs
            WHERE entity_type = 'customer_contract'
            """,
        ),
        (
            "合同来源订单",
            """
            SELECT COUNT(*)
            FROM sales_orders
            WHERE source_contract_id IS NOT NULL
            """,
        ),
    )
    for fact_name, query in fact_checks:
        if connection.execute(sa.text(query)).scalar_one():
            raise RuntimeError("N041 合同事实已存在，禁止破坏性降级；请恢复迁移前数据库备份")
    with op.batch_alter_table("sales_orders") as batch_op:
        batch_op.drop_constraint("uq_sales_orders_source_contract_id", type_="unique")
        batch_op.drop_constraint("fk_sales_orders_source_contract_id", type_="foreignkey")
        batch_op.drop_column("source_contract_id")
    op.drop_index("ix_customer_contract_items_product_id", table_name="customer_contract_items")
    op.drop_table("customer_contract_items")
    op.drop_index("ix_customer_contracts_status", table_name="customer_contracts")
    op.drop_index("ix_customer_contracts_customer_id", table_name="customer_contracts")
    op.drop_table("customer_contracts")
    op.drop_table("contract_daily_sequences")
