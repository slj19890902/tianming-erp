"""N043 email order intake facts and drafts.

Revision ID: cf62v8x9z51
Revises: ce61v8x9z50
"""

from alembic import op
import sqlalchemy as sa


revision = "cf62v8x9z51"
down_revision = "ce61v8x9z50"
branch_labels = None
depends_on = None


FACT_TABLES = (
    "email_order_sender_mappings",
    "email_order_intake_messages",
    "email_order_intake_attachments",
    "email_order_intake_drafts",
    "email_order_intake_poll_states",
)


def _assert_safe_downgrade() -> None:
    connection = op.get_bind()
    for table_name in FACT_TABLES:
        count = connection.execute(
            sa.text(f'SELECT COUNT(*) FROM "{table_name}"')
        ).scalar()
        if int(count or 0) > 0:
            raise RuntimeError(
                "N043 邮箱收单已存在配置、邮件或草稿事实；禁止破坏性降级，请恢复升级前数据库备份。"
            )


def upgrade() -> None:
    op.create_table(
        "email_order_intake_poll_states",
        sa.Column("mailbox_key", sa.String(100), nullable=False),
        sa.Column("last_started_at", sa.DateTime(), nullable=True),
        sa.Column("last_finished_at", sa.DateTime(), nullable=True),
        sa.Column("last_status", sa.String(20), server_default="never", nullable=False),
        sa.Column("examined_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("duplicate_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.CheckConstraint(
            "last_status IN ('never','running','success','failed')",
            name="ck_email_intake_poll_states_status",
        ),
        sa.PrimaryKeyConstraint("mailbox_key"),
    )

    op.create_table(
        "email_order_sender_mappings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("sender_email", sa.String(320), nullable=False),
        sa.Column("normalized_sender_email", sa.String(320), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("normalized_sender_email"),
    )
    op.create_index("ix_email_order_sender_mappings_customer_id", "email_order_sender_mappings", ["customer_id"])
    op.create_index("ix_email_order_sender_mappings_normalized_sender_email", "email_order_sender_mappings", ["normalized_sender_email"], unique=True)

    op.create_table(
        "email_order_intake_messages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("mailbox_key", sa.String(100), nullable=False),
        sa.Column("imap_uidvalidity", sa.String(100), nullable=False),
        sa.Column("imap_uid", sa.String(100), nullable=False),
        sa.Column("message_id_header", sa.String(500), nullable=True),
        sa.Column("sender_email", sa.String(320), nullable=False),
        sa.Column("normalized_sender_email", sa.String(320), nullable=False),
        sa.Column("subject", sa.String(500), nullable=True),
        sa.Column("received_at", sa.DateTime(), nullable=True),
        sa.Column("raw_message_sha256", sa.String(64), nullable=False),
        sa.Column("mapped_customer_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("status IN ('received','review_ready','needs_mapping','needs_confirmation','waiting_excel_parser','failed','ignored')", name="ck_email_intake_messages_status"),
        sa.ForeignKeyConstraint(["mapped_customer_id"], ["customers.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("mailbox_key", "imap_uidvalidity", "imap_uid", name="uq_email_intake_message_mailbox_uid"),
    )
    op.create_index("ix_email_order_intake_messages_mailbox_key", "email_order_intake_messages", ["mailbox_key"])
    op.create_index("ix_email_order_intake_messages_normalized_sender_email", "email_order_intake_messages", ["normalized_sender_email"])
    op.create_index("ix_email_order_intake_messages_mapped_customer_id", "email_order_intake_messages", ["mapped_customer_id"])
    op.create_index("ix_email_order_intake_messages_status", "email_order_intake_messages", ["status"])

    op.create_table(
        "email_order_intake_attachments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column("part_index", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(500), nullable=False),
        sa.Column("content_type", sa.String(200), nullable=True),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("file_sha256", sa.String(64), nullable=False),
        sa.Column("storage_path", sa.String(1000), nullable=True),
        sa.Column("file_type", sa.String(20), nullable=False),
        sa.Column("is_duplicate_content", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("duplicate_of_attachment_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.CheckConstraint("status IN ('stored','parsed','waiting_excel_parser','unsupported','failed')", name="ck_email_intake_attachments_status"),
        sa.CheckConstraint("file_type IN ('pdf','xls','xlsx','unsupported')", name="ck_email_intake_attachments_file_type"),
        sa.ForeignKeyConstraint(["message_id"], ["email_order_intake_messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["duplicate_of_attachment_id"], ["email_order_intake_attachments.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("message_id", "part_index", name="uq_email_intake_attachment_part"),
    )
    op.create_index("ix_email_order_intake_attachments_message_id", "email_order_intake_attachments", ["message_id"])
    op.create_index("ix_email_order_intake_attachments_file_sha256", "email_order_intake_attachments", ["file_sha256"])
    op.create_index("ix_email_order_intake_attachments_status", "email_order_intake_attachments", ["status"])

    op.create_table(
        "email_order_intake_drafts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column("attachment_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=True),
        sa.Column("parser_type", sa.String(20), nullable=False),
        sa.Column("parse_method", sa.String(40), nullable=True),
        sa.Column("parse_status", sa.String(80), nullable=True),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("draft_json", sa.Text(), nullable=True),
        sa.Column("source_name", sa.String(500), nullable=False),
        sa.Column("file_sha256", sa.String(64), nullable=False),
        sa.Column("converted_order_id", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("reviewed_by", sa.Integer(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("status IN ('review_ready','needs_mapping','needs_confirmation','waiting_excel_parser','failed','archived','converted')", name="ck_email_intake_drafts_status"),
        sa.CheckConstraint("parser_type IN ('pdf','xls','xlsx')", name="ck_email_intake_drafts_parser_type"),
        sa.ForeignKeyConstraint(["message_id"], ["email_order_intake_messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["attachment_id"], ["email_order_intake_attachments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["reviewed_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["converted_order_id"], ["sales_orders.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("attachment_id"),
        sa.UniqueConstraint("converted_order_id", name="uq_email_intake_drafts_converted_order_id"),
    )
    op.create_index("ix_email_order_intake_drafts_message_id", "email_order_intake_drafts", ["message_id"])
    op.create_index("ix_email_order_intake_drafts_attachment_id", "email_order_intake_drafts", ["attachment_id"], unique=True)
    op.create_index("ix_email_order_intake_drafts_customer_id", "email_order_intake_drafts", ["customer_id"])
    op.create_index("ix_email_order_intake_drafts_file_sha256", "email_order_intake_drafts", ["file_sha256"])
    op.create_index("ix_email_order_intake_drafts_status", "email_order_intake_drafts", ["status"])
    # A nullable unique index is portable across SQLite/PostgreSQL and makes
    # the email draft -> formal order association one-to-one in the database.
    with op.batch_alter_table("sales_orders", recreate="always") as batch_op:
        batch_op.add_column(
            sa.Column("email_intake_draft_id", sa.Integer(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_sales_orders_email_intake_draft_id",
            "email_order_intake_drafts",
            ["email_intake_draft_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    op.create_index(
        "uq_sales_orders_email_intake_draft_id",
        "sales_orders",
        ["email_intake_draft_id"],
        unique=True,
    )


def downgrade() -> None:
    _assert_safe_downgrade()
    op.drop_index("uq_sales_orders_email_intake_draft_id", table_name="sales_orders")
    with op.batch_alter_table("sales_orders", recreate="always") as batch_op:
        batch_op.drop_constraint(
            "fk_sales_orders_email_intake_draft_id", type_="foreignkey"
        )
        batch_op.drop_column("email_intake_draft_id")
    op.drop_table("email_order_intake_drafts")
    op.drop_table("email_order_intake_attachments")
    op.drop_table("email_order_intake_messages")
    op.drop_table("email_order_sender_mappings")
    op.drop_table("email_order_intake_poll_states")
