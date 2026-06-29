"""v0.20.8 splice mode and cutting mode snapshots

Revision ID: v84p1q2r3s72
Revises: u79o2p3q8r61
Create Date: 2026-06-29 00:00:01
"""

from alembic import op
import sqlalchemy as sa


revision = "v84p1q2r3s72"
down_revision = "u79o2p3q8r61"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("products", sa.Column("splice_mode", sa.String(length=20), nullable=True))
    op.add_column("products", sa.Column("pieces_per_box", sa.Integer(), nullable=True))
    op.add_column("products", sa.Column("flap_mm", sa.Integer(), nullable=True))

    op.add_column("sales_order_items", sa.Column("snapshot_splice_mode", sa.String(length=20), nullable=True))
    op.add_column("sales_order_items", sa.Column("snapshot_pieces_per_box", sa.Integer(), nullable=True))
    op.add_column("sales_order_items", sa.Column("snapshot_flap_mm", sa.Integer(), nullable=True))

    op.add_column("material_requisition_items", sa.Column("pieces_per_box", sa.Integer(), nullable=True))
    op.add_column("material_requisition_items", sa.Column("required_piece_qty", sa.Integer(), nullable=True))

    op.add_column("supplier_requisition_orders", sa.Column("cutting_mode", sa.String(length=30), nullable=True))
    op.add_column("supplier_requisition_orders", sa.Column("pieces_per_box", sa.Integer(), nullable=True))
    op.add_column("supplier_requisition_orders", sa.Column("required_piece_qty", sa.Integer(), nullable=True))

    op.add_column("supplier_requisition_order_items", sa.Column("cutting_mode", sa.String(length=30), nullable=True))
    op.add_column("supplier_requisition_order_items", sa.Column("pieces_per_box", sa.Integer(), nullable=True))
    op.add_column("supplier_requisition_order_items", sa.Column("required_piece_qty", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("supplier_requisition_order_items", "required_piece_qty")
    op.drop_column("supplier_requisition_order_items", "pieces_per_box")
    op.drop_column("supplier_requisition_order_items", "cutting_mode")

    op.drop_column("supplier_requisition_orders", "required_piece_qty")
    op.drop_column("supplier_requisition_orders", "pieces_per_box")
    op.drop_column("supplier_requisition_orders", "cutting_mode")

    op.drop_column("material_requisition_items", "required_piece_qty")
    op.drop_column("material_requisition_items", "pieces_per_box")

    op.drop_column("sales_order_items", "snapshot_flap_mm")
    op.drop_column("sales_order_items", "snapshot_pieces_per_box")
    op.drop_column("sales_order_items", "snapshot_splice_mode")

    op.drop_column("products", "flap_mm")
    op.drop_column("products", "pieces_per_box")
    op.drop_column("products", "splice_mode")
