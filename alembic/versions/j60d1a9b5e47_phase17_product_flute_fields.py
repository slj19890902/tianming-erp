"""Phase 17: 产品表新增楞型相关字段。

Revision ID: j60d1a9b5e47
Revises: i59f0a8b4c35
Create Date: 2026-06-24
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "j60d1a9b5e47"
down_revision: Union[str, Sequence[str], None] = "i59f0a8b4c35"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("products") as batch_op:
        batch_op.add_column(
            sa.Column("flute_type", sa.String(20), nullable=True)
        )
        batch_op.add_column(
            sa.Column("layer_count", sa.Integer, nullable=True)
        )
        batch_op.add_column(
            sa.Column("surface_paper_type", sa.String(20), nullable=True)
        )
        batch_op.add_column(
            sa.Column("legacy_flute_text", sa.Text, nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("products") as batch_op:
        batch_op.drop_column("legacy_flute_text")
        batch_op.drop_column("surface_paper_type")
        batch_op.drop_column("layer_count")
        batch_op.drop_column("flute_type")
