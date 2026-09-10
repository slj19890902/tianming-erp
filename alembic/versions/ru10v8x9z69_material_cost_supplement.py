"""Separate retrospective material cost references from actual purchases.

Revision ID: ru10v8x9z69
Revises: rt09v8x9z68
"""
from alembic import op
import sqlalchemy as sa

revision = "ru10v8x9z69"
down_revision = "rt09v8x9z68"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "finance_material_cost_supplements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("delivery_item_id", sa.Integer(), sa.ForeignKey("sales_delivery_items.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("inventory_lot_id", sa.Integer(), sa.ForeignKey("inventory_lots.id", ondelete="RESTRICT")),
        sa.Column("month", sa.String(7), nullable=False),
        sa.Column("source_kind", sa.String(40), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("target_fingerprint", sa.String(64), nullable=False),
        sa.Column("target_json", sa.Text(), nullable=False),
        sa.Column("quantity_limit", sa.Integer(), nullable=False),
        sa.Column("unit_cost", sa.Numeric(18, 6), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("reference_kind", sa.String(50), nullable=False),
        sa.Column("evidence_json", sa.Text(), nullable=False),
        sa.Column("evidence_fingerprint", sa.String(64), nullable=False),
        sa.Column("algorithm_version", sa.String(40), nullable=False),
        sa.Column("batch_id", sa.String(100), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.UniqueConstraint("target_fingerprint", name="uq_material_supplement_target"),
        sa.CheckConstraint("quantity_limit > 0 AND unit_cost > 0", name="ck_material_supplement_positive"),
        sa.CheckConstraint("currency = 'CNY'", name="ck_material_supplement_currency"),
        sa.CheckConstraint("length(target_fingerprint) = 64 AND length(evidence_fingerprint) = 64", name="ck_material_supplement_fingerprint"),
        sa.CheckConstraint("length(reason) > 0", name="ck_material_supplement_reason"),
    )
    for column in ("delivery_item_id", "month", "batch_id"):
        op.create_index(f"ix_finance_material_cost_supplements_{column}", "finance_material_cost_supplements", [column])
    # Append-only even for accidental SQL writes. Corrections require a separate
    # reviewed adjustment mechanism; never overwrite approved evidence.
    op.execute("CREATE TRIGGER material_supplement_no_update BEFORE UPDATE ON finance_material_cost_supplements BEGIN SELECT RAISE(ABORT, 'material supplement is immutable'); END")
    op.execute("CREATE TRIGGER material_supplement_no_delete BEFORE DELETE ON finance_material_cost_supplements BEGIN SELECT RAISE(ABORT, 'material supplement is immutable'); END")


def downgrade():
    if op.get_bind().execute(sa.text("SELECT COUNT(*) FROM finance_material_cost_supplements")).scalar():
        raise RuntimeError("Refusing to discard approved material supplements")
    op.execute("DROP TRIGGER material_supplement_no_update")
    op.execute("DROP TRIGGER material_supplement_no_delete")
    op.drop_table("finance_material_cost_supplements")
