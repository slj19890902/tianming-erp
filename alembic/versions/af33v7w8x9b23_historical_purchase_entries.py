"""store searchable historical purchase rows inside ERP

Revision ID: af33v7w8x9b23
Revises: ae32v7w8x9a22
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "af33v7w8x9b23"
down_revision = "ae32v7w8x9a22"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "historical_purchase_entries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_workbook", sa.String(length=260), nullable=False),
        sa.Column("source_sheet", sa.String(length=150), nullable=False),
        sa.Column("source_row", sa.Integer(), nullable=False),
        sa.Column("source_file_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("supplier_name", sa.String(length=150), nullable=True),
        sa.Column("record_date", sa.Date(), nullable=True),
        sa.Column("product_reference", sa.String(length=500), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.Column("normalized_search_text", sa.String(length=1000), nullable=False),
        sa.Column("material_code", sa.String(length=100), nullable=False),
        sa.Column("historical_quantity", sa.Integer(), nullable=True),
        sa.Column("report_length_mm", sa.Integer(), nullable=False),
        sa.Column("report_width_mm", sa.Integer(), nullable=False),
        sa.Column("crease_text", sa.String(length=250), nullable=True),
        sa.Column("crease_type", sa.String(length=30), nullable=True),
        sa.Column("crease_left_mm", sa.Integer(), nullable=True),
        sa.Column("crease_middle_mm", sa.Integer(), nullable=True),
        sa.Column("crease_right_mm", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("customer_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_workbook",
            "source_sheet",
            "source_row",
            name="uq_historical_purchase_entries_source_row",
        ),
    )
    op.create_index(
        "ix_historical_purchase_entries_source_fingerprint",
        "historical_purchase_entries",
        ["source_fingerprint"],
    )
    op.create_index(
        "ix_historical_purchase_entries_record_date",
        "historical_purchase_entries",
        ["record_date"],
    )
    op.create_index(
        "ix_historical_purchase_entries_normalized_search_text",
        "historical_purchase_entries",
        ["normalized_search_text"],
    )
    op.create_index(
        "ix_historical_purchase_entries_product_id",
        "historical_purchase_entries",
        ["product_id"],
    )
    op.create_index(
        "ix_historical_purchase_entries_customer_id",
        "historical_purchase_entries",
        ["customer_id"],
    )
    op.create_index(
        "ix_historical_purchase_entries_product_date",
        "historical_purchase_entries",
        ["product_id", "record_date"],
    )
    op.create_index(
        "ix_historical_purchase_entries_customer_date",
        "historical_purchase_entries",
        ["customer_id", "record_date"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_historical_purchase_entries_customer_date",
        table_name="historical_purchase_entries",
    )
    op.drop_index(
        "ix_historical_purchase_entries_product_date",
        table_name="historical_purchase_entries",
    )
    op.drop_index(
        "ix_historical_purchase_entries_customer_id",
        table_name="historical_purchase_entries",
    )
    op.drop_index(
        "ix_historical_purchase_entries_product_id",
        table_name="historical_purchase_entries",
    )
    op.drop_index(
        "ix_historical_purchase_entries_normalized_search_text",
        table_name="historical_purchase_entries",
    )
    op.drop_index(
        "ix_historical_purchase_entries_record_date",
        table_name="historical_purchase_entries",
    )
    op.drop_index(
        "ix_historical_purchase_entries_source_fingerprint",
        table_name="historical_purchase_entries",
    )
    op.drop_table("historical_purchase_entries")
