"""customer size-quotation preferences

Revision ID: cv78v8x9z67
Revises: ct76v8x9z65
Create Date: 2026-07-29
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "cv78v8x9z67"
down_revision: Union[str, Sequence[str], None] = "ct76v8x9z65"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Intentionally create an empty master-data table.  No historical product,
    # order, quotation, or material price is inferred or copied into it.
    op.create_table(
        "customer_quote_preferences",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("box_type", sa.String(length=150), nullable=False),
        sa.Column("material_id", sa.Integer(), nullable=False),
        sa.Column("flute_type", sa.String(length=20), nullable=False),
        sa.Column(
            "tax_included_square_price", sa.Numeric(precision=12, scale=4), nullable=False
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "tax_included_square_price > 0", name="ck_cqp_square_price"
        ),
        sa.CheckConstraint("version >= 1", name="ck_cqp_version"),
        sa.ForeignKeyConstraint(
            ["customer_id"], ["customers.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["material_id"], ["materials.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "customer_id",
            "box_type",
            "material_id",
            "flute_type",
            name="uq_customer_quote_preferences_identity",
        ),
    )
    op.create_index(
        "ix_customer_quote_preferences_customer_id",
        "customer_quote_preferences",
        ["customer_id"],
    )
    op.create_index(
        "ix_customer_quote_preferences_material_id",
        "customer_quote_preferences",
        ["material_id"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    has_facts = connection.execute(
        sa.text("SELECT 1 FROM customer_quote_preferences LIMIT 1")
    ).first()
    if has_facts is not None:
        raise RuntimeError(
            "存在客户尺寸报价偏好事实，禁止破坏性降级；请恢复迁移前数据库备份"
        )
    op.drop_index(
        "ix_customer_quote_preferences_material_id",
        table_name="customer_quote_preferences",
    )
    op.drop_index(
        "ix_customer_quote_preferences_customer_id",
        table_name="customer_quote_preferences",
    )
    op.drop_table("customer_quote_preferences")
