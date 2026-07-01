"""Corrugated material pricing rules and source-price metadata.

Revision ID: b19t6u7v8w18
Revises: a18t5u6v7w17
"""

from alembic import op
import sqlalchemy as sa


revision = "b19t6u7v8w18"
down_revision = "a18t5u6v7w17"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "supplier_material_base_prices",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("supplier_name", sa.String(200), nullable=False),
        sa.Column("material_code", sa.String(20), nullable=False),
        sa.Column("layer_count", sa.Integer(), nullable=False),
        sa.Column("base_price", sa.Numeric(12, 4), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("source", sa.String(250), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.UniqueConstraint("supplier_name", "material_code", "effective_date", name="uq_supplier_material_base_price"),
    )
    op.create_index("ix_supplier_material_base_prices_supplier_name", "supplier_material_base_prices", ["supplier_name"])
    op.create_table(
        "supplier_material_substitution_rules",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("supplier_name", sa.String(200), nullable=False),
        sa.Column("rule_type", sa.String(40), nullable=False),
        sa.Column("from_code", sa.String(1), nullable=False),
        sa.Column("to_code", sa.String(1), nullable=False),
        sa.Column("price_delta", sa.Numeric(12, 4), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("source", sa.String(250), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.UniqueConstraint("supplier_name", "rule_type", "from_code", "to_code", "effective_date", name="uq_supplier_material_substitution_rule"),
    )
    op.create_index("ix_supplier_material_substitution_rules_supplier_name", "supplier_material_substitution_rules", ["supplier_name"])
    op.create_table(
        "supplier_material_rule_configs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("supplier_name", sa.String(200), nullable=False),
        sa.Column("rule_key", sa.String(80), nullable=False),
        sa.Column("rule_value", sa.String(200)),
        sa.Column("status", sa.String(40), nullable=False, server_default="active"),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("participates_in_pricing", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime()),
        sa.UniqueConstraint("supplier_name", "rule_key", name="uq_supplier_material_rule_config"),
    )
    op.create_index("ix_supplier_material_rule_configs_supplier_name", "supplier_material_rule_configs", ["supplier_name"])
    with op.batch_alter_table("materials") as batch_op:
        batch_op.alter_column("flute_type", existing_type=sa.String(50), nullable=True)
        batch_op.add_column(sa.Column("rule_base_price", sa.Numeric(12, 4)))
        batch_op.add_column(sa.Column("price_source", sa.String(250)))


def downgrade() -> None:
    with op.batch_alter_table("materials") as batch_op:
        batch_op.drop_column("price_source")
        batch_op.drop_column("rule_base_price")
        batch_op.alter_column("flute_type", existing_type=sa.String(50), nullable=False, server_default="AB")
    op.drop_index("ix_supplier_material_rule_configs_supplier_name", table_name="supplier_material_rule_configs")
    op.drop_table("supplier_material_rule_configs")
    op.drop_index("ix_supplier_material_substitution_rules_supplier_name", table_name="supplier_material_substitution_rules")
    op.drop_table("supplier_material_substitution_rules")
    op.drop_index("ix_supplier_material_base_prices_supplier_name", table_name="supplier_material_base_prices")
    op.drop_table("supplier_material_base_prices")
