"""Persist trusted order-import sources and their line mappings."""

from alembic import op
import sqlalchemy as sa


revision = "du0920"
down_revision = "dt0920"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "order_import_sources",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source_kind", sa.String(length=30), nullable=False),
        sa.Column("source_key", sa.String(length=120), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("source_name_snapshot", sa.String(length=255), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("customer_po_snapshot", sa.String(length=150), nullable=True),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("email_attachment_id", sa.Integer(), nullable=True),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("confirmation_summary_json", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source_kind IN ('pdf_upload', 'email_attachment')",
            name="ck_order_import_sources_kind",
        ),
        sa.CheckConstraint(
            "length(source_hash) = 64 AND length(payload_hash) = 64",
            name="ck_order_import_sources_hashes",
        ),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["order_id"], ["sales_orders.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["email_attachment_id"],
            ["email_intake_attachments.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "source_kind", "source_key", name="uq_order_import_sources_kind_key"
        ),
        sa.UniqueConstraint("order_id", name="uq_order_import_sources_order_id"),
        sa.UniqueConstraint(
            "email_attachment_id",
            name="uq_order_import_sources_email_attachment_id",
        ),
    )
    op.create_index(
        "ix_order_import_sources_customer_po",
        "order_import_sources",
        ["customer_id", "customer_po_snapshot"],
    )
    op.create_index(
        "ix_order_import_sources_hash",
        "order_import_sources",
        ["source_hash"],
    )
    op.create_table(
        "order_import_source_lines",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("order_item_id", sa.Integer(), nullable=True),
        sa.Column("source_position", sa.Integer(), nullable=False),
        sa.Column("source_page", sa.Integer(), nullable=True),
        sa.Column("source_line_label", sa.String(length=80), nullable=True),
        sa.Column("raw_line_hash", sa.String(length=64), nullable=True),
        sa.Column("recognized_line_hash", sa.String(length=64), nullable=False),
        sa.Column("submitted_line_hash", sa.String(length=64), nullable=False),
        sa.Column("confirmation_summary_json", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "source_position >= 1",
            name="ck_order_import_source_lines_position",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"], ["order_import_sources.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["order_item_id"], ["sales_order_items.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint(
            "source_id",
            "source_position",
            name="uq_order_import_source_lines_position",
        ),
        sa.UniqueConstraint(
            "order_item_id",
            name="uq_order_import_source_lines_order_item_id",
        ),
    )
    op.create_index(
        "ix_order_import_source_lines_source_line",
        "order_import_source_lines",
        ["source_id", "source_line_label"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(
        sa.text("SELECT 1 FROM order_import_sources LIMIT 1")
    ).first():
        raise RuntimeError("已有订单导入来源事实，禁止删除；请使用已验证恢复备份")
    op.drop_index(
        "ix_order_import_source_lines_source_line",
        table_name="order_import_source_lines",
    )
    op.drop_table("order_import_source_lines")
    op.drop_index("ix_order_import_sources_hash", table_name="order_import_sources")
    op.drop_index(
        "ix_order_import_sources_customer_po",
        table_name="order_import_sources",
    )
    op.drop_table("order_import_sources")
