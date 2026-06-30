"""tianhua pre-delivery isolated tables

Revision ID: w95q2r3s4t83
Revises: v84p1q2r3s72
"""
from alembic import op
import sqlalchemy as sa
revision = "w95q2r3s4t83"
down_revision = "v84p1q2r3s72"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("tianhua_pre_delivery_import_batches",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("batch_number", sa.String(50), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False), sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("customer_name", sa.String(200), nullable=False), sa.Column("status", sa.String(30), nullable=False, server_default="preprocessed"),
        sa.Column("total_rows", sa.Integer(), nullable=False, server_default="0"), sa.Column("created_by", sa.Integer()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]), sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.CheckConstraint("status IN ('preprocessed','draft_created','failed')"), sa.UniqueConstraint("batch_number"))
    op.create_table("tianhua_pre_delivery_import_items",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("batch_id", sa.Integer(), nullable=False), sa.Column("row_no", sa.Integer(), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=False, server_default=""), sa.Column("stock_code", sa.String(30)), sa.Column("image_qty", sa.Integer()),
        sa.Column("product_id", sa.Integer()), sa.Column("product_name", sa.String(250)), sa.Column("order_item_id", sa.Integer()), sa.Column("order_number", sa.String(64)),
        sa.Column("system_pending_qty", sa.Integer()), sa.Column("available_qty", sa.Integer()), sa.Column("suggested_qty", sa.Integer()), sa.Column("final_delivery_qty", sa.Integer()),
        sa.Column("status", sa.String(30), nullable=False), sa.Column("warning", sa.Text()), sa.Column("selected", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["batch_id"], ["tianhua_pre_delivery_import_batches.id"], ondelete="CASCADE"), sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.ForeignKeyConstraint(["order_item_id"], ["sales_order_items.id"]), sa.CheckConstraint("status IN ('ok','duplicate_warning','qty_mismatch','stock_shortage','not_matched','ocr_failed')"),
        sa.UniqueConstraint("batch_id", "row_no"))
    op.create_index("ix_tianhua_import_batch", "tianhua_pre_delivery_import_items", ["batch_id"])
    op.create_table("tianhua_pre_delivery_drafts",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("draft_number", sa.String(50), nullable=False), sa.Column("batch_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False), sa.Column("status", sa.String(20), nullable=False, server_default="draft"), sa.Column("remark", sa.Text()),
        sa.Column("created_by", sa.Integer()), sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")), sa.Column("updated_at", sa.DateTime()),
        sa.ForeignKeyConstraint(["batch_id"], ["tianhua_pre_delivery_import_batches.id"]), sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]), sa.CheckConstraint("status='draft'"), sa.UniqueConstraint("batch_id"), sa.UniqueConstraint("draft_number"))
    op.create_table("tianhua_pre_delivery_draft_items",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("draft_id", sa.Integer(), nullable=False), sa.Column("import_item_id", sa.Integer(), nullable=False),
        sa.Column("row_no", sa.Integer(), nullable=False), sa.Column("stock_code", sa.String(30), nullable=False), sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("order_item_id", sa.Integer(), nullable=False), sa.Column("delivery_qty", sa.Integer(), nullable=False), sa.Column("warning", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["draft_id"], ["tianhua_pre_delivery_drafts.id"], ondelete="CASCADE"), sa.ForeignKeyConstraint(["import_item_id"], ["tianhua_pre_delivery_import_items.id"]),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]), sa.ForeignKeyConstraint(["order_item_id"], ["sales_order_items.id"]),
        sa.CheckConstraint("delivery_qty>0"), sa.UniqueConstraint("draft_id", "import_item_id"))


def downgrade():
    op.drop_table("tianhua_pre_delivery_draft_items")
    op.drop_table("tianhua_pre_delivery_drafts")
    op.drop_index("ix_tianhua_import_batch", table_name="tianhua_pre_delivery_import_items")
    op.drop_table("tianhua_pre_delivery_import_items")
    op.drop_table("tianhua_pre_delivery_import_batches")
