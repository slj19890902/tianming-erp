"""Box type component report formulas.

Revision ID: ab29u7v8w9x18
Revises: aa18t6u7v8w17
"""

from alembic import op
import sqlalchemy as sa


revision = "ab29u7v8w9x18"
down_revision = "aa18t6u7v8w17"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table_name, prefix in (
        ("products", ""),
        ("sales_order_items", "snapshot_"),
    ):
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.add_column(sa.Column(f"{prefix}base_report_length_mm", sa.Integer()))
            batch_op.add_column(sa.Column(f"{prefix}base_report_width_mm", sa.Integer()))
            batch_op.add_column(sa.Column(f"{prefix}base_crease_type", sa.String(20)))
            batch_op.add_column(sa.Column(f"{prefix}base_crease_left_mm", sa.Integer()))
            batch_op.add_column(sa.Column(f"{prefix}base_crease_middle_mm", sa.Integer()))
            batch_op.add_column(sa.Column(f"{prefix}base_crease_right_mm", sa.Integer()))
            batch_op.add_column(sa.Column(f"{prefix}base_report_notes", sa.Text()))


def downgrade() -> None:
    for table_name, prefix in (
        ("sales_order_items", "snapshot_"),
        ("products", ""),
    ):
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.drop_column(f"{prefix}base_report_notes")
            batch_op.drop_column(f"{prefix}base_crease_right_mm")
            batch_op.drop_column(f"{prefix}base_crease_middle_mm")
            batch_op.drop_column(f"{prefix}base_crease_left_mm")
            batch_op.drop_column(f"{prefix}base_crease_type")
            batch_op.drop_column(f"{prefix}base_report_width_mm")
            batch_op.drop_column(f"{prefix}base_report_length_mm")
