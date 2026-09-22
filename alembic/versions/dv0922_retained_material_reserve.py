"""Allow a fully inventory-covered order to retain explicit material reserve.

No historical rows change. Existing conservation checks force zero production
purpose whenever authoritative order demand is zero.
"""
from alembic import op
import sqlalchemy as sa

revision = "dv0922"
down_revision = "du0920"
branch_labels = None
depends_on = None

TABLE = "purchase_purpose_source_snapshots"
NAME = "ck_purchase_purpose_source_snapshots_group_conversion"
OLD = (
    "yield_per_sheet_snapshot > 0 AND group_effective_piece_qty_snapshot >= 0 "
    "AND group_effective_piece_qty_snapshot >= source_effective_piece_qty_snapshot "
    "AND group_authoritative_order_sheet_qty_snapshot > 0 "
    "AND group_authoritative_order_sheet_qty_snapshot * yield_per_sheet_snapshot "
    ">= group_effective_piece_qty_snapshot"
)
NEW = OLD.replace("group_authoritative_order_sheet_qty_snapshot > 0",
                  "group_authoritative_order_sheet_qty_snapshot >= 0")


def _replace(expression):
    conn = op.get_bind()
    triggers = []
    if conn.dialect.name == "sqlite":
        if conn.exec_driver_sql("PRAGMA foreign_keys").scalar():
            raise RuntimeError("Run the verified offline migration with SQLite FK disabled; verify foreign_key_check afterwards")
        triggers = conn.execute(sa.text(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND sql LIKE :pattern"
        ), {"pattern": f"%{TABLE}%"}).all()
        for name, _ in triggers:
            conn.exec_driver_sql('DROP TRIGGER "' + name.replace('"', '""') + '"')
    with op.batch_alter_table(TABLE) as batch:
        batch.drop_constraint(NAME, type_="check")
        batch.create_check_constraint(NAME, expression)
    for _, sql in triggers:
        conn.exec_driver_sql(sql)
    if conn.dialect.name == "sqlite" and conn.exec_driver_sql("PRAGMA foreign_key_check").first():
        raise RuntimeError("Foreign key check failed after reserve constraint migration")


def upgrade():
    _replace(NEW)


def downgrade():
    if op.get_bind().execute(sa.text(
        f"SELECT 1 FROM {TABLE} WHERE group_authoritative_order_sheet_qty_snapshot=0 LIMIT 1"
    )).first():
        raise RuntimeError("Retained reserve facts exist; refuse destructive downgrade")
    _replace(OLD)
