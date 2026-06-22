"""Add product trash lifecycle and versioned drawings.

Revision ID: f26b7d4a9c10
Revises: e13a6c4d2f40
Create Date: 2026-06-14
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f26b7d4a9c10"
down_revision: Union[str, Sequence[str], None] = "e13a6c4d2f40"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    product_columns = {
        column["name"] for column in inspector.get_columns("products")
    }

    if "deleted_at" not in product_columns:
        op.add_column(
            "products",
            sa.Column("deleted_at", sa.DateTime(), nullable=True),
        )
    if "deleted_by" not in product_columns:
        op.add_column(
            "products",
            sa.Column("deleted_by", sa.Integer(), nullable=True),
        )
    if "purged_at" not in product_columns:
        op.add_column(
            "products",
            sa.Column("purged_at", sa.DateTime(), nullable=True),
        )

    inspector = sa.inspect(connection)
    product_indexes = {
        index["name"] for index in inspector.get_indexes("products")
    }
    if "ix_products_deleted_at" not in product_indexes:
        op.create_index(
            "ix_products_deleted_at",
            "products",
            ["deleted_at"],
            unique=False,
        )
    if "ix_products_purged_at" not in product_indexes:
        op.create_index(
            "ix_products_purged_at",
            "products",
            ["purged_at"],
            unique=False,
        )

    tables = set(inspector.get_table_names())
    if "product_drawings" not in tables:
        op.create_table(
            "product_drawings",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("product_id", sa.Integer(), nullable=False),
            sa.Column("image_path", sa.Text(), nullable=False),
            sa.Column("thumbnail_path", sa.Text(), nullable=False),
            sa.Column(
                "uploaded_at",
                sa.DateTime(),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            sa.Column("uploaded_by", sa.Integer(), nullable=True),
            sa.ForeignKeyConstraint(
                ["product_id"],
                ["products.id"],
                ondelete="RESTRICT",
            ),
            sa.ForeignKeyConstraint(
                ["uploaded_by"],
                ["users.id"],
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_product_drawings_product_id",
            "product_drawings",
            ["product_id"],
            unique=False,
        )

    connection.execute(
        sa.text(
            """
            INSERT INTO product_drawings (
                product_id,
                image_path,
                thumbnail_path,
                uploaded_at,
                uploaded_by
            )
            SELECT
                products.id,
                products.drawing_path,
                products.drawing_path,
                COALESCE(
                    products.updated_at,
                    products.created_at,
                    CURRENT_TIMESTAMP
                ),
                NULL
            FROM products
            WHERE products.drawing_path IS NOT NULL
              AND TRIM(products.drawing_path) <> ''
              AND NOT EXISTS (
                  SELECT 1
                  FROM product_drawings
                  WHERE product_drawings.product_id = products.id
                    AND product_drawings.image_path = products.drawing_path
              )
            """
        )
    )


def downgrade() -> None:
    # Additive safety migration: destructive downgrade is intentionally disabled.
    pass
