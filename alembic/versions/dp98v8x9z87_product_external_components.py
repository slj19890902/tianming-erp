"""P1-33C1 common-box external packaging components.

Revision ID: dp98v8x9z87
Revises: do97v8x9z86
"""

from alembic import op
import sqlalchemy as sa


revision = "dp98v8x9z87"
down_revision = "do97v8x9z86"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("external_packaging_products") as batch_op:
        batch_op.add_column(
            sa.Column("customer_scope_id", sa.Integer(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_external_packaging_products_customer_scope",
            "customers",
            ["customer_scope_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    op.create_index(
        "ix_external_packaging_products_customer_scope_id",
        "external_packaging_products",
        ["customer_scope_id"],
    )

    op.create_table(
        "product_external_component_sets",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "product_id", "version", name="uq_product_external_component_set_version"
        ),
        sa.CheckConstraint(
            "version >= 1", name="ck_product_external_component_set_version"
        ),
    )
    op.create_index(
        "ix_product_external_component_sets_product_id",
        "product_external_component_sets",
        ["product_id"],
    )
    op.create_index(
        "uq_product_external_component_set_current",
        "product_external_component_sets",
        ["product_id"],
        unique=True,
        sqlite_where=sa.text("is_current = 1"),
        postgresql_where=sa.text("is_current = true"),
    )

    op.create_table(
        "product_external_components",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("component_set_id", sa.Integer(), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["component_set_id"],
            ["product_external_component_sets.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "component_set_id",
            "display_order",
            name="uq_product_external_component_order",
        ),
        sa.CheckConstraint(
            "quantity_per_finished_unit > 0",
            name="ck_product_external_component_quantity",
        ),
        sa.CheckConstraint(
            "waste_rate >= 0 AND waste_rate <= 1",
            name="ck_product_external_component_waste_rate",
        ),
        sa.CheckConstraint(
            "units_per_purchase_unit IS NULL OR units_per_purchase_unit > 0",
            name="ck_product_external_component_conversion",
        ),
    )
    op.create_index(
        "ix_product_external_components_component_set_id",
        "product_external_components",
        ["component_set_id"],
    )

    op.create_table(
        "product_external_component_candidates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("component_id", sa.Integer(), nullable=False),
        sa.Column("external_product_id", sa.Integer(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("supplier_id_snapshot", sa.Integer(), nullable=False),
        sa.Column("supplier_name_snapshot", sa.String(200), nullable=False),
        sa.Column("supplier_product_code_snapshot", sa.String(100), nullable=False),
        sa.Column("product_name_snapshot", sa.String(200), nullable=False),
        sa.Column("purchase_unit_snapshot", sa.String(20), nullable=False),
        sa.Column("customer_scope_id_snapshot", sa.Integer(), nullable=True),
        sa.Column("external_product_version_snapshot", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["component_id"], ["product_external_components.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["external_product_id"],
            ["external_packaging_products.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "component_id",
            "external_product_id",
            name="uq_product_external_component_candidate",
        ),
    )
    op.create_index(
        "ix_product_external_component_candidates_component_id",
        "product_external_component_candidates",
        ["component_id"],
    )
    op.create_index(
        "ix_product_external_component_candidates_external_product_id",
        "product_external_component_candidates",
        ["external_product_id"],
    )
    op.create_index(
        "uq_product_external_component_default_candidate",
        "product_external_component_candidates",
        ["component_id"],
        unique=True,
        sqlite_where=sa.text("is_default = 1"),
        postgresql_where=sa.text("is_default = true"),
    )


def downgrade() -> None:
    connection = op.get_bind()
    fact_count = connection.execute(
        sa.text("SELECT COUNT(*) FROM product_external_component_sets")
    ).scalar_one()
    scoped_count = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM external_packaging_products "
            "WHERE customer_scope_id IS NOT NULL"
        )
    ).scalar_one()
    if fact_count or scoped_count:
        raise RuntimeError(
            "P1-33C1 已存在常用箱外购组件或客户专用外购产品，禁止降级"
        )

    op.drop_index(
        "uq_product_external_component_default_candidate",
        table_name="product_external_component_candidates",
    )
    op.drop_index(
        "ix_product_external_component_candidates_external_product_id",
        table_name="product_external_component_candidates",
    )
    op.drop_index(
        "ix_product_external_component_candidates_component_id",
        table_name="product_external_component_candidates",
    )
    op.drop_table("product_external_component_candidates")
    op.drop_index(
        "ix_product_external_components_component_set_id",
        table_name="product_external_components",
    )
    op.drop_table("product_external_components")
    op.drop_index(
        "uq_product_external_component_set_current",
        table_name="product_external_component_sets",
    )
    op.drop_index(
        "ix_product_external_component_sets_product_id",
        table_name="product_external_component_sets",
    )
    op.drop_table("product_external_component_sets")
    op.drop_index(
        "ix_external_packaging_products_customer_scope_id",
        table_name="external_packaging_products",
    )
    with op.batch_alter_table("external_packaging_products") as batch_op:
        batch_op.drop_column("customer_scope_id")
