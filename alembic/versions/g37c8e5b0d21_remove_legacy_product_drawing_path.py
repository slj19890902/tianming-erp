"""Remove the legacy products drawing path after version migration.

Revision ID: g37c8e5b0d21
Revises: f26b7d4a9c10
Create Date: 2026-06-14
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "g37c8e5b0d21"
down_revision: Union[str, Sequence[str], None] = "f26b7d4a9c10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()
    columns = {
        column["name"]
        for column in sa.inspect(connection).get_columns("products")
    }
    if "drawing_path" not in columns:
        return

    missing = connection.scalar(
        sa.text(
            """
            SELECT COUNT(*)
            FROM products
            WHERE drawing_path IS NOT NULL
              AND TRIM(drawing_path) <> ''
              AND NOT EXISTS (
                  SELECT 1
                  FROM product_drawings
                  WHERE product_drawings.product_id = products.id
                    AND product_drawings.image_path = products.drawing_path
              )
            """
        )
    )
    if missing:
        raise RuntimeError(
            "Legacy product drawing paths have not all been migrated"
        )

    with op.batch_alter_table("products") as batch_op:
        batch_op.drop_column("drawing_path")


def downgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("products")
    }
    if "drawing_path" not in columns:
        op.add_column(
            "products",
            sa.Column("drawing_path", sa.Text(), nullable=True),
        )
