"""Add explicit cutting settings and frozen v2 snapshots; no data conversion."""
from alembic import op
import sqlalchemy as sa

revision = "eg1008sc"
down_revision = "ef1007cp"
branch_labels = None
depends_on = None

COLUMNS = {
    "products": "sheet_cutting_settings",
    "sales_order_items": "sheet_cutting_settings_snapshot",
    "sales_order_item_bom_components": "sheet_cutting_settings_snapshot",
    "material_requisition_items": "sheet_cutting_snapshot",
    "supplier_requisition_order_items": "sheet_cutting_snapshot",
    "stock_replenishment_order_items": "sheet_cutting_snapshot",
}
TRIGGER = "trg_bom_sheet_cutting_snapshot_immutable"


def upgrade():
    for table, column in COLUMNS.items():
        op.add_column(table, sa.Column(column, sa.JSON(none_as_null=True), nullable=True))
    if op.get_bind().dialect.name == "sqlite":
        op.execute(f'''CREATE TRIGGER {TRIGGER}
            BEFORE UPDATE OF sheet_cutting_settings_snapshot ON sales_order_item_bom_components
            WHEN OLD.sheet_cutting_settings_snapshot IS NOT NEW.sheet_cutting_settings_snapshot
            BEGIN SELECT RAISE(ABORT, 'BOM sheet cutting snapshot is immutable'); END''')


def downgrade():
    bind = op.get_bind()
    # Check every table before any DDL. v1 binaries cannot interpret v2 facts.
    for table, column in COLUMNS.items():
        if bind.execute(sa.text(f'SELECT 1 FROM "{table}" WHERE "{column}" IS NOT NULL LIMIT 1')).first():
            raise RuntimeError("开料 v2 数据已存在，禁止有损降级：" + table)
    if bind.dialect.name == "sqlite":
        op.execute(f"DROP TRIGGER IF EXISTS {TRIGGER}")
    for table, column in reversed(list(COLUMNS.items())):
        op.drop_column(table, column)
