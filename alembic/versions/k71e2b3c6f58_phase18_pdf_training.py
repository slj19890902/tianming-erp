"""Phase 18: PDF 订单识别训练样本库基础设施。

新增四张表：
- pdf_order_training_batches
- pdf_order_training_samples
- pdf_order_customer_templates
- pdf_order_correction_logs

Revision ID: k71e2b3c6f58
Revises: j60d1a9b5e47
Create Date: 2026-06-24
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "k71e2b3c6f58"
down_revision: Union[str, Sequence[str], None] = "j60d1a9b5e47"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. 批次表
    op.create_table(
        "pdf_order_training_batches",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("batch_name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("created_by", sa.String(100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime,
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.Column("notes", sa.Text, nullable=True),
    )

    # 2. 样本表
    op.create_table(
        "pdf_order_training_samples",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "batch_id",
            sa.Integer,
            sa.ForeignKey("pdf_order_training_batches.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "customer_id",
            sa.Integer,
            sa.ForeignKey("customers.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("file_name", sa.String(500), nullable=False),
        sa.Column("file_sha256", sa.String(64), nullable=False),
        sa.Column("file_path", sa.String(1000), nullable=True),
        sa.Column("parser_result_json", sa.Text, nullable=True),
        sa.Column("ground_truth_json", sa.Text, nullable=True),
        sa.Column("score", sa.Float, nullable=True),
        sa.Column("parse_status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("parse_method", sa.String(20), nullable=False, server_default="unknown"),
        sa.Column("extracted_text", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime,
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.Column("labeled_at", sa.DateTime, nullable=True),
        sa.Column("labeled_by", sa.String(100), nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.CheckConstraint(
            "parse_status IN ('pending', 'labeled', 'reviewed')",
            name="ck_pdf_sample_parse_status",
        ),
        sa.CheckConstraint(
            "parse_method IN ('text', 'ocr', 'failed', 'unknown')",
            name="ck_pdf_sample_parse_method",
        ),
    )
    op.create_index(
        "ix_pdf_samples_batch_id", "pdf_order_training_samples", ["batch_id"]
    )
    op.create_index(
        "ix_pdf_samples_customer_id", "pdf_order_training_samples", ["customer_id"]
    )
    op.create_index(
        "ix_pdf_samples_file_sha256", "pdf_order_training_samples", ["file_sha256"]
    )
    op.create_index(
        "ix_pdf_samples_parse_status", "pdf_order_training_samples", ["parse_status"]
    )

    # 3. 客户级模板表
    op.create_table(
        "pdf_order_customer_templates",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "customer_id",
            sa.Integer,
            sa.ForeignKey("customers.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("template_name", sa.String(200), nullable=False),
        sa.Column("order_no_pattern", sa.Text, nullable=True),
        sa.Column("date_pattern", sa.Text, nullable=True),
        sa.Column("item_row_pattern", sa.Text, nullable=True),
        sa.Column("customer_name_pattern", sa.Text, nullable=True),
        sa.Column("column_map_json", sa.Text, nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime,
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime, nullable=True),
        sa.Column("created_by", sa.String(100), nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
    )
    op.create_index(
        "ix_pdf_templates_customer_id",
        "pdf_order_customer_templates",
        ["customer_id"],
    )

    # 4. 纠错日志
    op.create_table(
        "pdf_order_correction_logs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "sample_id",
            sa.Integer,
            sa.ForeignKey("pdf_order_training_samples.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("field_path", sa.String(200), nullable=False),
        sa.Column("parser_value", sa.Text, nullable=True),
        sa.Column("corrected_value", sa.Text, nullable=True),
        sa.Column("corrected_by", sa.String(100), nullable=True),
        sa.Column(
            "corrected_at",
            sa.DateTime,
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.Column("note", sa.Text, nullable=True),
    )
    op.create_index(
        "ix_pdf_corrections_sample_id",
        "pdf_order_correction_logs",
        ["sample_id"],
    )


def downgrade() -> None:
    op.drop_table("pdf_order_correction_logs")
    op.drop_table("pdf_order_customer_templates")
    op.drop_table("pdf_order_training_samples")
    op.drop_table("pdf_order_training_batches")
