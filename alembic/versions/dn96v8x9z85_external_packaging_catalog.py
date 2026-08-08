"""add supplier categories and external packaging catalog

Revision ID: dn96v8x9z85
Revises: dm95v8x9z84
Create Date: 2026-08-09
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "dn96v8x9z85"
down_revision: Union[str, Sequence[str], None] = "dm95v8x9z84"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SUPPLIER_CATEGORIES = (
    "'corrugated_board','paper_corner_guard','coated_board',"
    "'printed_folding_carton','epe_cushion','other_packaging'"
)
EXTERNAL_CATEGORIES = (
    "'paper_corner_guard','coated_board','printed_folding_carton',"
    "'epe_cushion','other_packaging'"
)


def upgrade() -> None:
    op.create_table(
        "supplier_supply_categories",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("supplier_id", sa.Integer(), nullable=False),
        sa.Column("category_code", sa.String(length=50), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="1", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            f"category_code IN ({SUPPLIER_CATEGORIES})",
            name="ck_supplier_supply_categories_code",
        ),
        sa.ForeignKeyConstraint(
            ["supplier_id"], ["supplier_master_records.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "supplier_id",
            "category_code",
            name="uq_supplier_supply_categories_supplier_category",
        ),
    )
    op.create_index(
        "ix_supplier_supply_categories_supplier_id",
        "supplier_supply_categories",
        ["supplier_id"],
    )
    op.execute(
        sa.text(
            """
            INSERT INTO supplier_supply_categories(supplier_id, category_code, is_active)
            SELECT id, 'corrugated_board', 1
            FROM supplier_master_records
            """
        )
    )

    op.create_table(
        "external_packaging_products",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("supplier_id", sa.Integer(), nullable=False),
        sa.Column("category_code", sa.String(length=50), nullable=False),
        sa.Column("supplier_product_code", sa.String(length=100), nullable=False),
        sa.Column(
            "normalized_supplier_product_code", sa.String(length=100), nullable=False
        ),
        sa.Column("product_name", sa.String(length=200), nullable=False),
        sa.Column("purchase_unit", sa.String(length=20), nullable=False),
        sa.Column("specification_summary", sa.String(length=500), nullable=False),
        sa.Column("specification_json", sa.Text(), server_default="{}", nullable=False),
        sa.Column("drawing_sample_version", sa.String(length=100), nullable=True),
        sa.Column("lead_time_days", sa.Integer(), nullable=True),
        sa.Column("remarks", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            f"category_code IN ({EXTERNAL_CATEGORIES})",
            name="ck_external_packaging_products_category",
        ),
        sa.CheckConstraint(
            "lead_time_days IS NULL OR lead_time_days >= 0",
            name="ck_external_packaging_products_lead_time",
        ),
        sa.CheckConstraint("version >= 1", name="ck_external_packaging_products_version"),
        sa.ForeignKeyConstraint(
            ["supplier_id"], ["supplier_master_records.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "supplier_id",
            "normalized_supplier_product_code",
            name="uq_external_packaging_products_supplier_code",
        ),
    )
    op.create_index(
        "ix_external_packaging_products_supplier_id",
        "external_packaging_products",
        ["supplier_id"],
    )
    op.create_index(
        "ix_external_packaging_products_category_code",
        "external_packaging_products",
        ["category_code"],
    )
    op.create_index(
        "ix_external_packaging_products_is_active",
        "external_packaging_products",
        ["is_active"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    product_count = bind.execute(
        sa.text("SELECT COUNT(*) FROM external_packaging_products")
    ).scalar_one()
    changed_category_count = bind.execute(
        sa.text(
            """
            SELECT COUNT(*)
            FROM supplier_supply_categories
            WHERE category_code <> 'corrugated_board' OR is_active <> 1
            """
        )
    ).scalar_one()
    if product_count or changed_category_count:
        raise RuntimeError(
            "禁止破坏性降级：外购包装产品目录或供应商供货类别已产生业务事实"
        )
    op.drop_index(
        "ix_external_packaging_products_is_active",
        table_name="external_packaging_products",
    )
    op.drop_index(
        "ix_external_packaging_products_category_code",
        table_name="external_packaging_products",
    )
    op.drop_index(
        "ix_external_packaging_products_supplier_id",
        table_name="external_packaging_products",
    )
    op.drop_table("external_packaging_products")
    op.drop_index(
        "ix_supplier_supply_categories_supplier_id",
        table_name="supplier_supply_categories",
    )
    op.drop_table("supplier_supply_categories")
