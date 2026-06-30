"""Supplier paper-code dictionary for material composition.

Revision ID: a18t5u6v7w17
Revises: y17s4t5u6v05
"""

from alembic import op
import sqlalchemy as sa


revision = "a18t5u6v7w17"
down_revision = "y17s4t5u6v05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "supplier_paper_codes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("supplier_name", sa.String(length=200), nullable=False),
        sa.Column("code_char", sa.String(length=1), nullable=False),
        sa.Column("paper_name", sa.String(length=250), nullable=False),
        sa.Column("gram_weight", sa.Integer(), nullable=False),
        sa.Column("paper_grade", sa.String(length=100)),
        sa.Column("paper_role", sa.String(length=50)),
        sa.Column("remark", sa.Text()),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("updated_at", sa.DateTime()),
        sa.UniqueConstraint(
            "supplier_name",
            "code_char",
            name="uq_supplier_paper_codes_supplier_char",
        ),
    )
    op.create_index(
        "ix_supplier_paper_codes_supplier_name",
        "supplier_paper_codes",
        ["supplier_name"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_supplier_paper_codes_supplier_name",
        table_name="supplier_paper_codes",
    )
    op.drop_table("supplier_paper_codes")
