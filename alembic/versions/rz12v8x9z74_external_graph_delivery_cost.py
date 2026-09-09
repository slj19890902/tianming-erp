"""Real external receipt sources alongside unchanged paper receipt evidence."""
from alembic import op
import sqlalchemy as sa

revision = "rz12v8x9z74"
down_revision = "ry11v8x9z73"
branch_labels = None
depends_on = None

TABLE = "finance_delivery_graph_cost_portions"


def upgrade():
    with op.batch_alter_table(TABLE) as batch:
        batch.alter_column("purchase_receipt_fact_id", existing_type=sa.Integer(), nullable=True)
        batch.alter_column("purpose_allocation_id", existing_type=sa.Integer(), nullable=True)
        batch.add_column(sa.Column("external_receipt_item_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_graph_cost_external_receipt", "external_packaging_receipt_items",
                                 ["external_receipt_item_id"], ["id"], ondelete="RESTRICT")
        batch.create_check_constraint("ck_graph_cost_portion_source",
            "(purchase_receipt_fact_id IS NOT NULL AND purpose_allocation_id IS NOT NULL AND external_receipt_item_id IS NULL) OR "
            "(purchase_receipt_fact_id IS NULL AND purpose_allocation_id IS NULL AND external_receipt_item_id IS NOT NULL)")


def downgrade():
    if op.get_bind().execute(sa.text(f"SELECT 1 FROM {TABLE} WHERE external_receipt_item_id IS NOT NULL LIMIT 1")).first():
        raise RuntimeError("已有外购发货成本事实，禁止删除；请使用已验证备份回退")
    with op.batch_alter_table(TABLE) as batch:
        batch.drop_constraint("ck_graph_cost_portion_source", type_="check")
        batch.drop_constraint("fk_graph_cost_external_receipt", type_="foreignkey")
        batch.drop_column("external_receipt_item_id")
        batch.alter_column("purchase_receipt_fact_id", existing_type=sa.Integer(), nullable=False)
        batch.alter_column("purpose_allocation_id", existing_type=sa.Integer(), nullable=False)
