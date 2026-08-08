"""P1-33C3 human-confirmed external-packaging purchase documents.

Revision ID: dr00v8x9z89
Revises: dq99v8x9z88
"""

from alembic import op
import sqlalchemy as sa


revision = "dr00v8x9z89"
down_revision = "dq99v8x9z88"
branch_labels = None
depends_on = None


SEQUENCE_TABLE = "external_packaging_purchase_daily_sequences"
BATCH_TABLE = "external_packaging_purchase_batches"
ORDER_TABLE = "external_packaging_purchase_orders"
ITEM_TABLE = "external_packaging_purchase_items"
FACT_TABLES = (BATCH_TABLE, ORDER_TABLE, ITEM_TABLE)


def _create_immutable_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for table_name in FACT_TABLES:
            for action in ("UPDATE", "DELETE"):
                op.execute(
                    f"""
                    CREATE TRIGGER trg_{table_name}_immutable_{action.lower()}
                    BEFORE {action} ON {table_name}
                    FOR EACH ROW
                    BEGIN
                        SELECT RAISE(ABORT, '{table_name} rows are immutable');
                    END
                    """
                )
    elif dialect == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION p1_33c3_immutable_external_purchase()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'external packaging purchase facts are immutable';
            END;
            $$
            """
        )
        for table_name in FACT_TABLES:
            op.execute(
                f"""
                CREATE TRIGGER trg_{table_name}_immutable_write
                BEFORE UPDATE OR DELETE ON {table_name}
                FOR EACH ROW EXECUTE FUNCTION p1_33c3_immutable_external_purchase()
                """
            )


def _drop_immutable_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for table_name in reversed(FACT_TABLES):
            for action in ("delete", "update"):
                op.execute(
                    f"DROP TRIGGER IF EXISTS trg_{table_name}_immutable_{action}"
                )
    elif dialect == "postgresql":
        for table_name in reversed(FACT_TABLES):
            op.execute(
                f"DROP TRIGGER IF EXISTS trg_{table_name}_immutable_write ON {table_name}"
            )
        op.execute(
            "DROP FUNCTION IF EXISTS p1_33c3_immutable_external_purchase()"
        )


def upgrade() -> None:
    op.create_table(
        SEQUENCE_TABLE,
        sa.Column("sequence_date", sa.Date(), primary_key=True),
        sa.Column("last_value", sa.Integer(), nullable=False),
    )
    op.create_table(
        BATCH_TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("sales_order_id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(120), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("confirmed_by", sa.Integer(), nullable=True),
        sa.Column(
            "confirmed_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.ForeignKeyConstraint(
            ["sales_order_id"], ["sales_orders.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["confirmed_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "sales_order_id", name="uq_external_packaging_purchase_batch_order"
        ),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_external_packaging_purchase_batch_key"
        ),
    )
    op.create_index(
        "ix_external_packaging_purchase_batches_sales_order_id",
        BATCH_TABLE,
        ["sales_order_id"],
    )
    op.create_table(
        ORDER_TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("batch_id", sa.Integer(), nullable=False),
        sa.Column("purchase_number", sa.String(40), nullable=False),
        sa.Column("supplier_id", sa.Integer(), nullable=False),
        sa.Column("supplier_name_snapshot", sa.String(200), nullable=False),
        sa.Column("supplier_business_code_snapshot", sa.String(50), nullable=True),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="confirmed"),
        sa.Column("goods_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("tax_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("total_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("confirmed_by", sa.Integer(), nullable=True),
        sa.Column(
            "confirmed_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.ForeignKeyConstraint(["batch_id"], [f"{BATCH_TABLE}.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["supplier_id"], ["supplier_master_records.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["confirmed_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "batch_id",
            "supplier_id",
            name="uq_external_packaging_purchase_batch_supplier",
        ),
        sa.UniqueConstraint(
            "purchase_number", name="uq_external_packaging_purchase_number"
        ),
        sa.CheckConstraint(
            "status IN ('confirmed')", name="ck_external_packaging_purchase_status"
        ),
        sa.CheckConstraint(
            "goods_amount >= 0", name="ck_external_packaging_purchase_goods_amount"
        ),
        sa.CheckConstraint(
            "tax_amount >= 0", name="ck_external_packaging_purchase_tax_amount"
        ),
        sa.CheckConstraint(
            "total_amount >= 0", name="ck_external_packaging_purchase_total_amount"
        ),
    )
    op.create_index(
        "ix_external_packaging_purchase_orders_batch_id", ORDER_TABLE, ["batch_id"]
    )
    op.create_table(
        ITEM_TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("purchase_order_id", sa.Integer(), nullable=False),
        sa.Column("sales_order_id", sa.Integer(), nullable=False),
        sa.Column("sales_order_item_id", sa.Integer(), nullable=False),
        sa.Column("order_component_id", sa.Integer(), nullable=False),
        sa.Column("order_candidate_id", sa.Integer(), nullable=False),
        sa.Column("purpose_snapshot", sa.String(200), nullable=False),
        sa.Column("category_code_snapshot", sa.String(50), nullable=False),
        sa.Column("specification_summary_snapshot", sa.String(500), nullable=False),
        sa.Column("external_product_id_snapshot", sa.Integer(), nullable=False),
        sa.Column("external_product_version_snapshot", sa.Integer(), nullable=False),
        sa.Column("supplier_product_code_snapshot", sa.String(100), nullable=False),
        sa.Column("product_name_snapshot", sa.String(200), nullable=False),
        sa.Column("price_version_id", sa.Integer(), nullable=False),
        sa.Column("price_version_number_snapshot", sa.Integer(), nullable=False),
        sa.Column("purchase_quantity", sa.Numeric(18, 6), nullable=False),
        sa.Column("purchase_unit", sa.String(20), nullable=False),
        sa.Column("unit_price", sa.Numeric(18, 6), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("tax_mode", sa.String(20), nullable=False),
        sa.Column("tax_rate", sa.Numeric(8, 6), nullable=False),
        sa.Column("tax_amount_per_unit", sa.Numeric(18, 6), nullable=True),
        sa.Column("line_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("tax_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("total_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("tier_basis_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("moq_quantity_snapshot", sa.Numeric(18, 4), nullable=True),
        sa.Column("packaging_multiple_snapshot", sa.Numeric(18, 4), nullable=True),
        sa.Column("shipping_fee_mode", sa.String(20), nullable=False),
        sa.Column("shipping_fee_snapshot", sa.Numeric(18, 6), nullable=True),
        sa.Column("sample_fee_snapshot", sa.Numeric(18, 6), nullable=True),
        sa.Column("plate_fee_snapshot", sa.Numeric(18, 6), nullable=True),
        sa.Column("die_fee_snapshot", sa.Numeric(18, 6), nullable=True),
        sa.Column("price_evidence_reference_snapshot", sa.String(500), nullable=False),
        sa.Column(
            "confirmed_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.ForeignKeyConstraint(
            ["purchase_order_id"], [f"{ORDER_TABLE}.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["sales_order_id"], ["sales_orders.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["sales_order_item_id"], ["sales_order_items.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["order_component_id"],
            ["sales_order_item_external_components.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["order_candidate_id"],
            ["sales_order_item_external_component_candidates.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["price_version_id"],
            ["external_packaging_price_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "order_component_id", name="uq_external_packaging_purchase_component"
        ),
        sa.CheckConstraint(
            "purchase_quantity > 0", name="ck_external_packaging_purchase_item_qty"
        ),
        sa.CheckConstraint(
            "unit_price > 0", name="ck_external_packaging_purchase_item_price"
        ),
        sa.CheckConstraint(
            "tax_rate >= 0 AND tax_rate <= 1",
            name="ck_external_packaging_purchase_item_tax_rate",
        ),
        sa.CheckConstraint(
            "tax_amount_per_unit IS NULL OR tax_amount_per_unit >= 0",
            name="ck_external_packaging_purchase_item_tax_unit",
        ),
        sa.CheckConstraint(
            "line_amount >= 0 AND tax_amount >= 0 AND total_amount >= 0",
            name="ck_external_packaging_purchase_item_amounts",
        ),
        sa.CheckConstraint(
            "tax_mode IN ('tax_inclusive','tax_exclusive')",
            name="ck_external_packaging_purchase_item_tax_mode",
        ),
        sa.CheckConstraint(
            "shipping_fee_mode IN ('not_provided','included','per_order','per_unit')",
            name="ck_external_packaging_purchase_item_shipping_mode",
        ),
    )
    op.create_index(
        "ix_external_packaging_purchase_items_purchase_order_id",
        ITEM_TABLE,
        ["purchase_order_id"],
    )
    op.create_index(
        "ix_external_packaging_purchase_items_sales_order_id",
        ITEM_TABLE,
        ["sales_order_id"],
    )
    _create_immutable_guards()


def downgrade() -> None:
    connection = op.get_bind()
    counts = {
        table_name: int(
            connection.execute(
                sa.text(f"SELECT COUNT(*) FROM {table_name}")
            ).scalar_one()
            or 0
        )
        for table_name in FACT_TABLES
    }
    if any(counts.values()):
        raise RuntimeError(
            "P1-33C3 已存在正式外购包装采购事实，禁止破坏性降级；请恢复升级前备份"
        )
    _drop_immutable_guards()
    op.drop_index(
        "ix_external_packaging_purchase_items_sales_order_id", table_name=ITEM_TABLE
    )
    op.drop_index(
        "ix_external_packaging_purchase_items_purchase_order_id",
        table_name=ITEM_TABLE,
    )
    op.drop_table(ITEM_TABLE)
    op.drop_index(
        "ix_external_packaging_purchase_orders_batch_id", table_name=ORDER_TABLE
    )
    op.drop_table(ORDER_TABLE)
    op.drop_index(
        "ix_external_packaging_purchase_batches_sales_order_id",
        table_name=BATCH_TABLE,
    )
    op.drop_table(BATCH_TABLE)
    op.drop_table(SEQUENCE_TABLE)
