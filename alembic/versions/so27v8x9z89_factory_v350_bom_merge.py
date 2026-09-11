"""Join formal cost/mail history and the unpublished BOM candidate.

No DDL, backfill, or version stamping. The candidate graph-cost revision was
renamed rx11v8x9z72 to avoid the formal rx10v8x9z72 identity collision.
"""
from alembic import op
import sqlalchemy as sa

revision = "so27v8x9z89"
down_revision = ("sn26v8x9z88", "rz10v8x9z74")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    # Reject before Alembic changes merge metadata if either branch holds facts
    # whose lower migrations would reject a destructive downgrade.
    db = op.get_bind()
    for table in ("order_bom_rule_revisions", "order_bom_source_handoffs",
                  "order_bom_external_receipt_executions", "inventory_cost_rules",
                  "inventory_cost_mutations", "email_intake_settings",
                  "email_intake_messages", "email_intake_attachments",
                  "email_intake_order_links"):
        if db.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError(f"已有BOM、库存成本或邮件事实（{table}），须恢复已验证备份，不得降级删除事实")
