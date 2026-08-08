"""add immutable external packaging price versions

Revision ID: do97v8x9z86
Revises: dn96v8x9z85
Create Date: 2026-08-09
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "do97v8x9z86"
down_revision: Union[str, Sequence[str], None] = "dn96v8x9z85"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "external_packaging_price_versions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("external_product_id", sa.Integer(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("product_version", sa.Integer(), nullable=False),
        sa.Column("specification_snapshot_json", sa.Text(), nullable=False),
        sa.Column("quote_unit", sa.String(length=20), nullable=False),
        sa.Column("unit_conversion_basis", sa.Text(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("tax_mode", sa.String(length=20), nullable=False),
        sa.Column("tax_rate", sa.Numeric(8, 6), nullable=False),
        sa.Column("tax_amount_per_unit", sa.Numeric(18, 6), nullable=True),
        sa.Column("unit_price", sa.Numeric(18, 6), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("moq_quantity", sa.Numeric(18, 4), nullable=True),
        sa.Column("moq_unit", sa.String(length=20), nullable=True),
        sa.Column("packaging_multiple", sa.Numeric(18, 4), nullable=True),
        sa.Column("tier_prices_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column("shipping_fee_mode", sa.String(length=20), nullable=False),
        sa.Column("shipping_fee", sa.Numeric(18, 6), nullable=True),
        sa.Column("sample_fee", sa.Numeric(18, 6), nullable=True),
        sa.Column("plate_fee", sa.Numeric(18, 6), nullable=True),
        sa.Column("die_fee", sa.Numeric(18, 6), nullable=True),
        sa.Column("evidence_reference", sa.String(length=500), nullable=False),
        sa.Column("remarks", sa.Text(), nullable=True),
        sa.Column("quote_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("version_number >= 1", name="ck_external_packaging_price_version"),
        sa.CheckConstraint("product_version >= 1", name="ck_external_packaging_price_product_version"),
        sa.CheckConstraint("unit_price > 0", name="ck_external_packaging_price_unit_price"),
        sa.CheckConstraint("tax_rate >= 0 AND tax_rate <= 1", name="ck_external_packaging_price_tax_rate"),
        sa.CheckConstraint("tax_amount_per_unit IS NULL OR tax_amount_per_unit >= 0", name="ck_external_packaging_price_tax_amount"),
        sa.CheckConstraint("moq_quantity IS NULL OR moq_quantity > 0", name="ck_external_packaging_price_moq"),
        sa.CheckConstraint("packaging_multiple IS NULL OR packaging_multiple > 0", name="ck_external_packaging_price_multiple"),
        sa.CheckConstraint("shipping_fee IS NULL OR shipping_fee >= 0", name="ck_external_packaging_price_shipping"),
        sa.CheckConstraint("sample_fee IS NULL OR sample_fee >= 0", name="ck_external_packaging_price_sample_fee"),
        sa.CheckConstraint("plate_fee IS NULL OR plate_fee >= 0", name="ck_external_packaging_price_plate_fee"),
        sa.CheckConstraint("die_fee IS NULL OR die_fee >= 0", name="ck_external_packaging_price_die_fee"),
        sa.CheckConstraint("effective_to IS NULL OR effective_to >= effective_from", name="ck_external_packaging_price_effective_range"),
        sa.CheckConstraint("tax_mode IN ('tax_inclusive','tax_exclusive')", name="ck_external_packaging_price_tax_mode"),
        sa.CheckConstraint("shipping_fee_mode IN ('not_provided','included','per_order','per_unit')", name="ck_external_packaging_price_shipping_mode"),
        sa.ForeignKeyConstraint(
            ["external_product_id"], ["external_packaging_products.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "external_product_id",
            "version_number",
            name="uq_external_packaging_price_product_version",
        ),
        sa.UniqueConstraint(
            "external_product_id",
            "quote_fingerprint",
            name="uq_external_packaging_price_product_fingerprint",
        ),
    )
    op.create_index(
        "ix_external_packaging_price_versions_external_product_id",
        "external_packaging_price_versions",
        ["external_product_id"],
    )
    op.create_index(
        "ix_external_packaging_price_effective_lookup",
        "external_packaging_price_versions",
        ["external_product_id", "effective_from", "effective_to", "version_number"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    count = bind.execute(
        sa.text("SELECT COUNT(*) FROM external_packaging_price_versions")
    ).scalar_one()
    if count:
        raise RuntimeError(
            "禁止破坏性降级：外购包装真实报价版本已产生业务事实"
        )
    op.drop_index(
        "ix_external_packaging_price_effective_lookup",
        table_name="external_packaging_price_versions",
    )
    op.drop_index(
        "ix_external_packaging_price_versions_external_product_id",
        table_name="external_packaging_price_versions",
    )
    op.drop_table("external_packaging_price_versions")
