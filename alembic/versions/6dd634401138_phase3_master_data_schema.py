"""Phase 3 master data schema without touching historical tables.

Revision ID: 6dd634401138
Revises: 1a03b26f44c4
Create Date: 2026-06-13
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "6dd634401138"
down_revision: Union[str, Sequence[str], None] = "1a03b26f44c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _create_customers() -> None:
    op.create_table(
        "customers",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("customer_number", sa.Integer(), nullable=True),
        sa.Column("customer_code", sa.String(length=50), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column(
            "payment_term_days",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "credit_limit",
            sa.Numeric(precision=14, scale=2),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("contact_person", sa.String(length=100), nullable=True),
        sa.Column("phone", sa.String(length=100), nullable=True),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("billing_note", sa.Text(), nullable=True),
        sa.Column("credit_terms", sa.String(length=100), nullable=True),
        sa.Column(
            "default_tax_rate",
            sa.Numeric(precision=6, scale=4),
            server_default=sa.text("0.13"),
            nullable=False,
        ),
        sa.Column("invoice_title", sa.String(length=200), nullable=True),
        sa.Column("tax_no", sa.String(length=100), nullable=True),
        sa.Column("bank_account", sa.String(length=200), nullable=True),
        sa.Column("remark", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=30),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column(
            "is_active",
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )


def _add_customer_fields(bind) -> None:
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("customers")}
    with op.batch_alter_table("customers") as batch:
        if "customer_number" not in columns:
            batch.add_column(sa.Column("customer_number", sa.Integer(), nullable=True))
        if "payment_term_days" not in columns:
            batch.add_column(
                sa.Column(
                    "payment_term_days",
                    sa.Integer(),
                    server_default=sa.text("0"),
                    nullable=False,
                )
            )
        if "credit_limit" not in columns:
            batch.add_column(
                sa.Column(
                    "credit_limit",
                    sa.Numeric(precision=14, scale=2),
                    server_default=sa.text("0"),
                    nullable=False,
                )
            )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    if "customers" not in table_names:
        _create_customers()
    else:
        _add_customer_fields(bind)

    customer_indexes = {
        index["name"] for index in sa.inspect(bind).get_indexes("customers")
    }
    if "ux_customers_customer_number" not in customer_indexes:
        op.create_index(
            "ux_customers_customer_number",
            "customers",
            ["customer_number"],
            unique=True,
        )
    if "ux_customers_customer_code" not in customer_indexes:
        op.create_index(
            "ux_customers_customer_code",
            "customers",
            ["customer_code"],
            unique=True,
        )

    if "materials" not in table_names:
        op.create_table(
            "materials",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("code", sa.String(length=100), nullable=False),
            sa.Column("paper_composition", sa.String(length=200), nullable=True),
            sa.Column("layer_count", sa.Integer(), nullable=True),
            sa.Column("flute_type", sa.String(length=50), nullable=True),
            sa.Column("basis_weight_description", sa.Text(), nullable=True),
            sa.Column(
                "quote_price",
                sa.Numeric(precision=12, scale=4),
                nullable=True,
            ),
            sa.Column("price_unit", sa.String(length=50), nullable=True),
            sa.Column("supplier_name", sa.String(length=200), nullable=True),
            sa.Column("quote_date", sa.Date(), nullable=True),
            sa.Column("remarks", sa.Text(), nullable=True),
            sa.Column(
                "is_active",
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
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("code"),
        )
        op.create_index("ix_materials_code", "materials", ["code"], unique=True)

    if "products" not in table_names:
        op.create_table(
            "products",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("customer_id", sa.Integer(), nullable=False),
            sa.Column("product_code", sa.String(length=150), nullable=False),
            sa.Column(
                "customer_material_code",
                sa.String(length=150),
                nullable=False,
            ),
            sa.Column("product_name", sa.String(length=250), nullable=False),
            sa.Column("material_id", sa.Integer(), nullable=True),
            sa.Column("legacy_material_text", sa.String(length=250), nullable=True),
            sa.Column("length_mm", sa.Numeric(precision=12, scale=2), nullable=True),
            sa.Column("width_mm", sa.Numeric(precision=12, scale=2), nullable=True),
            sa.Column("height_mm", sa.Numeric(precision=12, scale=2), nullable=True),
            sa.Column(
                "box_category",
                sa.String(length=20),
                server_default=sa.text("'normal'"),
                nullable=False,
            ),
            sa.Column("box_style", sa.String(length=150), nullable=True),
            sa.Column("print_content", sa.Text(), nullable=True),
            sa.Column("printing_colors", sa.String(length=150), nullable=True),
            sa.Column("production_process", sa.Text(), nullable=True),
            sa.Column(
                "unit",
                sa.String(length=20),
                server_default=sa.text("'只'"),
                nullable=False,
            ),
            sa.Column(
                "sale_unit_price",
                sa.Numeric(precision=12, scale=4),
                nullable=True,
            ),
            sa.Column(
                "sale_unit_price_no_tax",
                sa.Numeric(precision=12, scale=4),
                nullable=True,
            ),
            sa.Column(
                "cost_unit_price",
                sa.Numeric(precision=12, scale=4),
                nullable=True,
            ),
            sa.Column(
                "board_price",
                sa.Numeric(precision=12, scale=4),
                nullable=True,
            ),
            sa.Column(
                "suggested_price",
                sa.Numeric(precision=12, scale=4),
                nullable=True,
            ),
            sa.Column("drawing_path", sa.Text(), nullable=True),
            sa.Column("die_cut_path", sa.Text(), nullable=True),
            sa.Column("remark", sa.Text(), nullable=True),
            sa.Column(
                "is_active",
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
                "box_category IN ('normal', 'die_cut')",
                name="ck_products_box_category",
            ),
            sa.ForeignKeyConstraint(
                ["customer_id"],
                ["customers.id"],
                ondelete="RESTRICT",
            ),
            sa.ForeignKeyConstraint(
                ["material_id"],
                ["materials.id"],
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "customer_id",
                "customer_material_code",
                name="uq_products_customer_material_code",
            ),
            sa.UniqueConstraint(
                "customer_id",
                "product_code",
                name="uq_products_customer_product_code",
            ),
        )
        op.create_index(
            "ix_products_customer_id",
            "products",
            ["customer_id"],
            unique=False,
        )
        op.create_index(
            "ix_products_material_id",
            "products",
            ["material_id"],
            unique=False,
        )

    if "migration_entity_map" not in table_names:
        op.create_table(
            "migration_entity_map",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("source_system", sa.String(length=50), nullable=False),
            sa.Column("entity_type", sa.String(length=50), nullable=False),
            sa.Column("source_id", sa.String(length=100), nullable=False),
            sa.Column("target_table", sa.String(length=100), nullable=False),
            sa.Column("target_id", sa.Integer(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "source_system",
                "entity_type",
                "source_id",
                name="uq_migration_entity_source",
            ),
        )
        op.create_index(
            "ix_migration_entity_map_source_system",
            "migration_entity_map",
            ["source_system"],
            unique=False,
        )
        op.create_index(
            "ix_migration_entity_map_entity_type",
            "migration_entity_map",
            ["entity_type"],
            unique=False,
        )
        op.create_index(
            "ix_migration_entity_map_target_id",
            "migration_entity_map",
            ["target_id"],
            unique=False,
        )


def downgrade() -> None:
    # Phase 3 is non-destructive. Master data and mappings are retained.
    return
