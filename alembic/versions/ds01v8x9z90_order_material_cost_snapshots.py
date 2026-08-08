"""P1-28B immutable order-item material cost snapshots.

Revision ID: ds01v8x9z90
Revises: dr00v8x9z89
"""

from alembic import op
import sqlalchemy as sa


revision = "ds01v8x9z90"
down_revision = "dr00v8x9z89"
branch_labels = None
depends_on = None


TABLE = "sales_order_item_material_cost_snapshots"


def _create_immutable_guard() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(
                f"""
                CREATE TRIGGER trg_{TABLE}_immutable_{action.lower()}
                BEFORE {action} ON {TABLE}
                FOR EACH ROW
                BEGIN
                    SELECT RAISE(ABORT, '{TABLE} rows are immutable');
                END
                """
            )
    elif dialect == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION p1_28b_immutable_material_cost_snapshot()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'order item material cost snapshots are immutable';
            END;
            $$
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER trg_{TABLE}_immutable_write
            BEFORE UPDATE OR DELETE ON {TABLE}
            FOR EACH ROW EXECUTE FUNCTION p1_28b_immutable_material_cost_snapshot()
            """
        )


def _drop_immutable_guard() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for action in ("delete", "update"):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{TABLE}_immutable_{action}")
    elif dialect == "postgresql":
        op.execute(f"DROP TRIGGER IF EXISTS trg_{TABLE}_immutable_write ON {TABLE}")
        op.execute("DROP FUNCTION IF EXISTS p1_28b_immutable_material_cost_snapshot()")


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("sales_order_item_id", sa.Integer(), nullable=False),
        sa.Column("order_item_reference_snapshot", sa.String(100), nullable=False),
        sa.Column("snapshot_version", sa.Integer(), nullable=False),
        sa.Column("source_fingerprint", sa.String(64), nullable=False),
        sa.Column("calculation_status", sa.String(20), nullable=False),
        sa.Column("scope_code", sa.String(30), nullable=False),
        sa.Column("formula_version", sa.String(50), nullable=False),
        sa.Column("precision_version", sa.String(50), nullable=False),
        sa.Column("order_quantity_snapshot", sa.Integer(), nullable=False),
        sa.Column("estimated_material_unit_cost", sa.Numeric(18, 6), nullable=True),
        sa.Column("estimated_material_total_cost", sa.Numeric(18, 2), nullable=True),
        sa.Column("known_material_subtotal", sa.Numeric(18, 2), nullable=True),
        sa.Column("components_json", sa.Text(), nullable=False),
        sa.Column("missing_items_json", sa.Text(), nullable=False),
        sa.Column(
            "calculated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.UniqueConstraint(
            "order_item_reference_snapshot",
            "snapshot_version",
            name="uq_order_item_material_cost_snapshot_version",
        ),
        sa.UniqueConstraint(
            "order_item_reference_snapshot",
            "source_fingerprint",
            name="uq_order_item_material_cost_snapshot_fingerprint",
        ),
        sa.CheckConstraint(
            "snapshot_version > 0", name="ck_order_item_material_cost_snapshot_version"
        ),
        sa.CheckConstraint(
            "order_quantity_snapshot > 0",
            name="ck_order_item_material_cost_snapshot_quantity",
        ),
        sa.CheckConstraint(
            "calculation_status IN ('calculated','partial','missing')",
            name="ck_order_item_material_cost_snapshot_status",
        ),
        sa.CheckConstraint(
            "scope_code = 'material_only'",
            name="ck_order_item_material_cost_snapshot_scope",
        ),
    )
    op.create_index(
        "ix_sales_order_item_material_cost_snapshots_sales_order_item_id",
        TABLE,
        ["sales_order_item_id"],
    )
    op.create_index(
        "ix_sales_order_item_material_cost_snapshots_order_item_reference_snapshot",
        TABLE,
        ["order_item_reference_snapshot"],
    )
    _create_immutable_guard()


def downgrade() -> None:
    count = int(
        op.get_bind().execute(sa.text(f"SELECT COUNT(*) FROM {TABLE}")).scalar_one()
        or 0
    )
    if count:
        raise RuntimeError(
            "P1-28B 已存在订单材料成本历史快照，禁止破坏性降级；请恢复升级前备份"
        )
    _drop_immutable_guard()
    op.drop_index(
        "ix_sales_order_item_material_cost_snapshots_order_item_reference_snapshot",
        table_name=TABLE,
    )
    op.drop_index(
        "ix_sales_order_item_material_cost_snapshots_sales_order_item_id",
        table_name=TABLE,
    )
    op.drop_table(TABLE)
