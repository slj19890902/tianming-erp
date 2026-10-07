"""Enable hundredth-mm card-stock dimensions; never rewrite historical facts.

SQLite INTEGER affinity accepts REAL values losslessly. No table rebuild is
needed there: retain integer rows, all existing triggers and indexes. Other
engines require NUMERIC columns. A new revision prevents unsafe code rollback
once fractional facts exist.
"""
from alembic import op
import sqlalchemy as sa

revision = "ef1007cp"
down_revision = "ee0930ba"
branch_labels = None
depends_on = None

COLUMNS = {'inventory_onboarding_lines': ['board_length_mm', 'board_width_mm'], 'sales_order_items': ['snapshot_report_length_mm', 'snapshot_report_width_mm', 'snapshot_base_report_length_mm', 'snapshot_base_report_width_mm'], 'products': ['report_length_mm', 'report_width_mm', 'base_report_length_mm', 'base_report_width_mm'], 'sales_order_item_bom_components': ['snapshot_component_report_length_mm', 'snapshot_component_report_width_mm', 'snapshot_component_base_report_length_mm', 'snapshot_component_base_report_width_mm'], 'inventory_stock_policies': ['report_length_mm', 'report_width_mm'], 'stock_replenishment_order_items': ['report_length_mm', 'report_width_mm'], 'supplier_requisition_orders': ['report_length_mm', 'report_width_mm'], 'supplier_requisition_order_items': ['report_length_mm', 'report_width_mm'], 'semi_finished_inventory_details': ['board_length_mm', 'board_width_mm'], 'order_item_semi_requirements': ['board_length_mm', 'board_width_mm'], 'semi_finished_match_rules': ['board_length_mm', 'board_width_mm']}

def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        for table, columns in COLUMNS.items():
            existing = {c["name"] for c in sa.inspect(bind).get_columns(table)}
            if not set(columns) <= existing:
                raise RuntimeError("Missing dimension columns: " + table)
        return
    for table, columns in COLUMNS.items():
        for column in columns:
            op.alter_column(table, column, existing_type=sa.Integer(), type_=sa.Numeric(12, 2))

def downgrade():
    bind = op.get_bind()
    for table, columns in COLUMNS.items():
        condition = " OR ".join(f'("{col}" IS NOT NULL AND "{col}" != CAST("{col}" AS INTEGER))' for col in columns)
        if bind.execute(sa.text(f'SELECT 1 FROM "{table}" WHERE {condition} LIMIT 1')).first():
            raise RuntimeError("Fractional sheet dimensions exist; refuse lossy downgrade: " + table)
    if bind.dialect.name != "sqlite":
        for table, columns in COLUMNS.items():
            for column in columns:
                op.alter_column(table, column, existing_type=sa.Numeric(12, 2), type_=sa.Integer())
