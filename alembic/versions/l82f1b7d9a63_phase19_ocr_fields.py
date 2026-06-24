"""phase19: add ocr_text_raw + expand parse_method constraint

Revision ID: l82f1b7d9a63
Revises: k71e2b3c6f58
Create Date: 2026-06-24

新增：
  pdf_order_training_samples.ocr_text_raw  TEXT  (OCR 识别结果)
  pdf_order_training_samples.parse_method  扩展允许值（ocr_easyocr / ocr_tesseract /
                                            ocr_unavailable / ocr_failed / mixed）
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "l82f1b7d9a63"
down_revision = "k71e2b3c6f58"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite 不支持 ALTER COLUMN，改约束需通过 batch_alter_table 重建
    with op.batch_alter_table("pdf_order_training_samples") as batch_op:
        # 新增 ocr_text_raw 列
        batch_op.add_column(sa.Column("ocr_text_raw", sa.Text, nullable=True))
        # 删除旧 parse_method 约束，重建扩展版本
        batch_op.drop_constraint(
            "ck_pdf_sample_parse_method", type_="check"
        )
        batch_op.create_check_constraint(
            "ck_pdf_sample_parse_method",
            "parse_method IN ('text', 'ocr', 'failed', 'unknown', 'mixed', "
            "'ocr_easyocr', 'ocr_tesseract', 'ocr_unavailable', 'ocr_failed')",
        )


def downgrade() -> None:
    with op.batch_alter_table("pdf_order_training_samples") as batch_op:
        batch_op.drop_column("ocr_text_raw")
        batch_op.drop_constraint(
            "ck_pdf_sample_parse_method", type_="check"
        )
        batch_op.create_check_constraint(
            "ck_pdf_sample_parse_method",
            "parse_method IN ('text', 'ocr', 'failed', 'unknown')",
        )
