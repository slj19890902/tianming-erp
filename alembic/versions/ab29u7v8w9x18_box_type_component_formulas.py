"""Box type component report formulas.

Revision ID: ab29u7v8w9x18
Revises: aa18t6u7v8w17
Create Date: 2026-07-08
"""

from alembic import op
import sqlalchemy as sa


revision = "ab29u7v8w9x18"
down_revision = "aa18t6u7v8w17"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("products") as batch_op:
        batch_op.add_column(sa.Column("base_report_length_mm", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("base_report_width_mm", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("base_crease_type", sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column("base_crease_left_mm", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("base_crease_middle_mm", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("base_crease_right_mm", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("base_report_notes", sa.Text(), nullable=True))

    with op.batch_alter_table("sales_order_items") as batch_op:
        batch_op.add_column(sa.Column("snapshot_base_report_length_mm", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("snapshot_base_report_width_mm", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("snapshot_base_crease_type", sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column("snapshot_base_crease_left_mm", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("snapshot_base_crease_middle_mm", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("snapshot_base_crease_right_mm", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("snapshot_base_report_notes", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("sales_order_items") as batch_op:
        batch_op.drop_column("snapshot_base_report_notes")
        batch_op.drop_column("snapshot_base_crease_right_mm")
        batch_op.drop_column("snapshot_base_crease_middle_mm")
        batch_op.drop_column("snapshot_base_crease_left_mm")
        batch_op.drop_column("snapshot_base_crease_type")
        batch_op.drop_column("snapshot_base_report_width_mm")
        batch_op.drop_column("snapshot_base_report_length_mm")

    with op.batch_alter_table("products") as batch_op:
        batch_op.drop_column("base_report_notes")
        batch_op.drop_column("base_crease_right_mm")
        batch_op.drop_column("base_crease_middle_mm")
        batch_op.drop_column("base_crease_left_mm")
        batch_op.drop_column("base_crease_type")
        batch_op.drop_column("base_report_width_mm")
        batch_op.drop_column("base_report_length_mm")
