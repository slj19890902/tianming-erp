"""Audited cost-only recipes for stocktake entries; no business data backfill."""
from alembic import op
import sqlalchemy as sa

revision = "rx10v8x9z72"
down_revision = "rw10v8x9z71"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("inventory_cost_rules",
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("config_json", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.CheckConstraint("version > 0", name="ck_inventory_cost_rules_version"))
    op.create_table("inventory_cost_mutations",
        sa.Column("batch_id", sa.String(64), primary_key=True),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("request_json", sa.Text(), nullable=False),
        sa.Column("response_json", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()))


def downgrade():
    if any(op.get_bind().execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first()
            for table in ("inventory_cost_rules", "inventory_cost_mutations")):
        raise RuntimeError("Cost rule facts exist; retain the schema or restore a verified backup")
    op.drop_table("inventory_cost_mutations")
    op.drop_table("inventory_cost_rules")
