"""P1-33C2 order external-packaging snapshots and quantity suggestions.

Revision ID: dq99v8x9z88
Revises: dp98v8x9z87
"""

from alembic import op
import sqlalchemy as sa


revision = "dq99v8x9z88"
down_revision = "dp98v8x9z87"
branch_labels = None
depends_on = None


COMPONENT_TABLE = "sales_order_item_external_components"
CANDIDATE_TABLE = "sales_order_item_external_component_candidates"


def _create_immutable_guards() -> None:
    connection = op.get_bind()
    dialect = connection.dialect.name
    if dialect == "sqlite":
        for table_name in (COMPONENT_TABLE, CANDIDATE_TABLE):
            op.execute(
                f"""
                CREATE TRIGGER trg_{table_name}_immutable_update
                BEFORE UPDATE ON {table_name}
                FOR EACH ROW
                BEGIN
                    SELECT RAISE(ABORT, '{table_name} rows are immutable');
                END
                """
            )
    elif dialect == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION p1_33c2_immutable_order_external_snapshot()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'order external packaging snapshots are immutable';
            END;
            $$
            """
        )
        for table_name in (COMPONENT_TABLE, CANDIDATE_TABLE):
            op.execute(
                f"""
                CREATE TRIGGER trg_{table_name}_immutable_update
                BEFORE UPDATE ON {table_name}
                FOR EACH ROW EXECUTE FUNCTION p1_33c2_immutable_order_external_snapshot()
                """
            )


def _drop_immutable_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for table_name in (CANDIDATE_TABLE, COMPONENT_TABLE):
            op.execute(
                f"DROP TRIGGER IF EXISTS trg_{table_name}_immutable_update"
            )
    elif dialect == "postgresql":
        for table_name in (CANDIDATE_TABLE, COMPONENT_TABLE):
            op.execute(
                f"DROP TRIGGER IF EXISTS trg_{table_name}_immutable_update "
                f"ON {table_name}"
            )
        op.execute(
            "DROP FUNCTION IF EXISTS p1_33c2_immutable_order_external_snapshot()"
        )


def upgrade() -> None:
    op.create_table(
        COMPONENT_TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("sales_order_item_id", sa.Integer(), nullable=False),
        sa.Column("source_component_set_id", sa.Integer(), nullable=False),
        sa.Column("source_component_id", sa.Integer(), nullable=False),
        sa.Column("source_component_set_version", sa.Integer(), nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.Column("purpose", sa.String(200), nullable=False),
        sa.Column("quantity_per_finished_unit", sa.Numeric(18, 6), nullable=False),
        sa.Column("waste_rate", sa.Numeric(8, 6), nullable=False, server_default="0"),
        sa.Column("consumption_unit", sa.String(20), nullable=False),
        sa.Column("units_per_purchase_unit", sa.Numeric(18, 6), nullable=True),
        sa.Column("conversion_basis", sa.String(500), nullable=True),
        sa.Column("is_required", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("remarks", sa.Text(), nullable=True),
        sa.Column("category_code", sa.String(50), nullable=False),
        sa.Column("specification_json", sa.Text(), nullable=False),
        sa.Column("specification_summary", sa.String(500), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.ForeignKeyConstraint(
            ["sales_order_item_id"], ["sales_order_items.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["source_component_set_id"],
            ["product_external_component_sets.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_component_id"],
            ["product_external_components.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "sales_order_item_id",
            "source_component_id",
            name="uq_sales_order_item_external_component_source",
        ),
        sa.UniqueConstraint(
            "sales_order_item_id",
            "display_order",
            name="uq_sales_order_item_external_component_order",
        ),
        sa.CheckConstraint(
            "source_component_set_version >= 1",
            name="ck_sales_order_item_external_component_set_version",
        ),
        sa.CheckConstraint(
            "quantity_per_finished_unit > 0",
            name="ck_sales_order_item_external_component_quantity",
        ),
        sa.CheckConstraint(
            "waste_rate >= 0 AND waste_rate <= 1",
            name="ck_sales_order_item_external_component_waste_rate",
        ),
        sa.CheckConstraint(
            "units_per_purchase_unit IS NULL OR units_per_purchase_unit > 0",
            name="ck_sales_order_item_external_component_conversion",
        ),
    )
    op.create_index(
        "ix_sales_order_item_external_components_order_item_id",
        COMPONENT_TABLE,
        ["sales_order_item_id"],
    )

    op.create_table(
        CANDIDATE_TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("order_component_id", sa.Integer(), nullable=False),
        sa.Column("source_candidate_id", sa.Integer(), nullable=False),
        sa.Column("external_product_id_snapshot", sa.Integer(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("supplier_id_snapshot", sa.Integer(), nullable=False),
        sa.Column("supplier_name_snapshot", sa.String(200), nullable=False),
        sa.Column("supplier_product_code_snapshot", sa.String(100), nullable=False),
        sa.Column("product_name_snapshot", sa.String(200), nullable=False),
        sa.Column("purchase_unit_snapshot", sa.String(20), nullable=False),
        sa.Column("customer_scope_id_snapshot", sa.Integer(), nullable=True),
        sa.Column("external_product_version_snapshot", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["order_component_id"], [f"{COMPONENT_TABLE}.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["source_candidate_id"],
            ["product_external_component_candidates.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "order_component_id",
            "source_candidate_id",
            name="uq_sales_order_item_external_candidate_source",
        ),
    )
    op.create_index(
        "ix_sales_order_item_external_candidates_component_id",
        CANDIDATE_TABLE,
        ["order_component_id"],
    )
    op.create_index(
        "uq_sales_order_item_external_candidate_default",
        CANDIDATE_TABLE,
        ["order_component_id"],
        unique=True,
        sqlite_where=sa.text("is_default = 1"),
        postgresql_where=sa.text("is_default = true"),
    )
    _create_immutable_guards()


def downgrade() -> None:
    connection = op.get_bind()
    count = int(
        connection.execute(
            sa.text(f"SELECT COUNT(*) FROM {COMPONENT_TABLE}")
        ).scalar_one()
        or 0
    )
    if count:
        raise RuntimeError(
            "P1-33C2 已存在订单外购组件快照，禁止破坏性降级；请恢复升级前备份"
        )
    _drop_immutable_guards()
    op.drop_index(
        "uq_sales_order_item_external_candidate_default",
        table_name=CANDIDATE_TABLE,
    )
    op.drop_index(
        "ix_sales_order_item_external_candidates_component_id",
        table_name=CANDIDATE_TABLE,
    )
    op.drop_table(CANDIDATE_TABLE)
    op.drop_index(
        "ix_sales_order_item_external_components_order_item_id",
        table_name=COMPONENT_TABLE,
    )
    op.drop_table(COMPONENT_TABLE)
