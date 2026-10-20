"""Freeze owner-authorized historical delivery receivable sources."""
from alembic import op
import sqlalchemy as sa

revision = "ev1020hr"
down_revision = "eu1020cs"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("finance_customer_charges", sa.Column("historical_source_key", sa.String(120), nullable=True))
    op.add_column("finance_customer_charges", sa.Column("historical_delivery_snapshot_json", sa.Text(), nullable=True))
    op.create_index("uq_customer_charge_historical_source", "finance_customer_charges", ["historical_source_key"], unique=True)


def downgrade():
    if op.get_bind().execute(sa.text(
        "SELECT 1 FROM finance_customer_charges WHERE historical_source_key IS NOT NULL "
        "OR historical_delivery_snapshot_json IS NOT NULL LIMIT 1"
    )).first():
        raise RuntimeError("已有历史送货应收来源，禁止丢失来源降级")
    op.drop_index("uq_customer_charge_historical_source", table_name="finance_customer_charges")
    op.drop_column("finance_customer_charges", "historical_delivery_snapshot_json")
    op.drop_column("finance_customer_charges", "historical_source_key")
