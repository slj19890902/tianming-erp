"""External receipt cancellation evidence; no existing facts modified."""
from alembic import op
import sqlalchemy as sa

revision = "sa13v8x9z75"
down_revision = "rz12v8x9z74"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("external_packaging_receipt_reversals",
        sa.Column("receipt_id", sa.Integer(), sa.ForeignKey("external_packaging_receipts.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("idempotency_key", sa.String(120), nullable=False, unique=True),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("reversed_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("reversed_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.CheckConstraint("length(trim(reason)) > 0 AND length(idempotency_key) > 0 AND length(request_fingerprint) = 64", name="ck_external_receipt_reversal_text"))


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM external_packaging_receipt_reversals LIMIT 1")).first():
        raise RuntimeError("已有外购实收撤销事实，禁止删除；请使用已验证备份回退")
    op.drop_table("external_packaging_receipt_reversals")
