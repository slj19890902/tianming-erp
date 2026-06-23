"""Phase 13 historical requisition mapping.

Revision ID: e13a6c4d2f40
Revises: d12f7a3c9b20
Create Date: 2026-06-14
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e13a6c4d2f40"
down_revision: Union[str, Sequence[str], None] = "d12f7a3c9b20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    product_columns = {
        column["name"] for column in inspector.get_columns("products")
    }
    additions = (
        ("default_cardboard_length", sa.Numeric(12, 2)),
        ("default_cardboard_width", sa.Numeric(12, 2)),
        ("default_score_lines", sa.String(250)),
        ("default_material_code", sa.String(100)),
    )
    for name, column_type in additions:
        if name not in product_columns:
            op.add_column(
                "products",
                sa.Column(name, column_type, nullable=True),
            )

    inspector = sa.inspect(connection)
    if not inspector.has_table("historical_requisition_maps"):
        op.create_table(
            "historical_requisition_maps",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("product_id", sa.Integer(), nullable=True),
            sa.Column("search_key", sa.String(length=500), nullable=False),
            sa.Column(
                "normalized_search_key",
                sa.String(length=500),
                nullable=False,
            ),
            sa.Column(
                "cardboard_length",
                sa.Numeric(precision=12, scale=2),
                nullable=False,
            ),
            sa.Column(
                "cardboard_width",
                sa.Numeric(precision=12, scale=2),
                nullable=False,
            ),
            sa.Column("score_lines", sa.String(length=250), nullable=True),
            sa.Column("material_code", sa.String(length=100), nullable=False),
            sa.Column("quantity", sa.Integer(), nullable=True),
            sa.Column("record_date", sa.Date(), nullable=True),
            sa.Column("source_workbook", sa.String(length=260), nullable=False),
            sa.Column("source_sheet", sa.String(length=150), nullable=False),
            sa.Column("source_row", sa.Integer(), nullable=False),
            sa.Column("raw_data", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                server_default=sa.func.current_timestamp(),
                nullable=False,
            ),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(
                ["product_id"],
                ["products.id"],
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "normalized_search_key",
                name="uq_historical_requisition_maps_normalized_key",
            ),
        )
        op.create_index(
            "ix_historical_requisition_maps_product_id",
            "historical_requisition_maps",
            ["product_id"],
            unique=False,
        )
        op.create_index(
            "ix_historical_requisition_maps_search_key",
            "historical_requisition_maps",
            ["search_key"],
            unique=False,
        )


def downgrade() -> None:
    # Safety policy: production history migrations do not perform destructive rollback.
    pass

