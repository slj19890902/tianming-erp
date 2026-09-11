"""Preserve formal PDF working drafts alongside the BOM candidate. No DDL."""
from alembic import op
import sqlalchemy as sa

revision = "sp28v8x9z90"
down_revision = ("so27v8x9z89", "sb11v8x9z76")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    db = op.get_bind()
    for table in ("email_pdf_working_drafts", "order_bom_rule_revisions",
                  "order_bom_source_handoffs", "order_bom_external_receipt_executions",
                  "inventory_cost_rules", "inventory_cost_mutations", "email_intake_settings",
                  "email_intake_messages", "email_intake_attachments", "email_intake_order_links"):
        if db.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError(f"已有BOM、库存成本或邮件事实（{table}），须恢复已验证备份，不得降级删除事实")
