"""customer quote preference crease type

Revision ID: el04w9y0a93
Revises: dk93v8x9z82
Create Date: 2026-08-06
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "el04w9y0a93"
down_revision: Union[str, Sequence[str], None] = "dk93v8x9z82"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing customer quote preferences were created when the only manual-size
    # A1 path used the confirmed pressure-crease recommendation.  Preserve that
    # meaning explicitly instead of leaving a nullable/ambiguous legacy value.
    with op.batch_alter_table("customer_quote_preferences") as batch_op:
        batch_op.add_column(
            sa.Column(
                "crease_type",
                sa.String(length=20),
                nullable=False,
                server_default="压线",
            )
        )
        batch_op.drop_constraint(
            "uq_customer_quote_preferences_identity", type_="unique"
        )
        batch_op.create_unique_constraint(
            "uq_customer_quote_preferences_identity",
            [
                "customer_id",
                "box_type",
                "crease_type",
                "material_id",
                "flute_type",
            ],
        )


def downgrade() -> None:
    connection = op.get_bind()
    non_default_fact = connection.execute(
        sa.text(
            "SELECT 1 FROM customer_quote_preferences "
            "WHERE crease_type <> '压线' LIMIT 1"
        )
    ).first()
    if non_default_fact is not None:
        raise RuntimeError(
            "存在非压线客户报价偏好事实，禁止破坏性降级；请恢复迁移前数据库备份"
        )
    with op.batch_alter_table("customer_quote_preferences") as batch_op:
        batch_op.drop_constraint(
            "uq_customer_quote_preferences_identity", type_="unique"
        )
        batch_op.create_unique_constraint(
            "uq_customer_quote_preferences_identity",
            ["customer_id", "box_type", "material_id", "flute_type"],
        )
        batch_op.drop_column("crease_type")
