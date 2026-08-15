"""persist physical pallet versus logical location-anchor semantics

Revision ID: nn22v8x9z11
Revises: mm21v8x9z10
Create Date: 2026-08-14
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "nn22v8x9z11"
down_revision = "mm21v8x9z10"
branch_labels = None
depends_on = None


def _replace_sqlite_unplace_guard(*, require_positive_balance: bool) -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_warehouse_locations_unplace_reference_guard")
    balance_guard = (
        " AND (lot.quantity_available + lot.quantity_reserved "
        "+ lot.quantity_damaged) > 0"
        if require_positive_balance
        else ""
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER trg_warehouse_locations_unplace_reference_guard
            BEFORE UPDATE OF placement_status, is_active ON warehouse_locations
            FOR EACH ROW
            WHEN (NEW.placement_status <> 'placed' OR NEW.is_active <> 1)
             AND (
                EXISTS (
                    SELECT 1 FROM inventory_lots lot
                    WHERE lot.warehouse_location_id = NEW.id
                      AND lot.status IN ('active','frozen')
                      {balance_guard}
                )
                OR EXISTS (
                    SELECT 1 FROM inventory_pallets pallet
                    WHERE pallet.location_id = NEW.id
                      AND pallet.is_current = 1
                )
             )
            BEGIN
                SELECT RAISE(ABORT, 'referenced warehouse location cannot be unplaced or disabled');
            END
            """.format(balance_guard=balance_guard)
        )
    )


def _replace_sqlite_inventory_location_guards(
    *, require_positive_balance: bool
) -> None:
    for trigger in (
        "trg_inventory_lots_require_placed_location_update",
        "trg_inventory_lots_require_placed_location_insert",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    balance_guard = (
        " AND (NEW.quantity_available + NEW.quantity_reserved "
        "+ NEW.quantity_damaged) > 0"
        if require_positive_balance
        else ""
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER trg_inventory_lots_require_placed_location_insert
            BEFORE INSERT ON inventory_lots
            FOR EACH ROW
            WHEN NEW.status IN ('active','frozen')
             {balance_guard}
             AND NOT EXISTS (
                SELECT 1 FROM warehouse_locations location
                WHERE location.id = NEW.warehouse_location_id
                  AND location.is_active = 1
                  AND location.placement_status = 'placed'
             )
            BEGIN
                SELECT RAISE(ABORT, 'inventory lot requires an active placed location');
            END
            """.format(balance_guard=balance_guard)
        )
    )
    update_columns = (
        "warehouse_location_id, status, quantity_available, "
        "quantity_reserved, quantity_damaged"
        if require_positive_balance
        else "warehouse_location_id, status"
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER trg_inventory_lots_require_placed_location_update
            BEFORE UPDATE OF {update_columns} ON inventory_lots
            FOR EACH ROW
            WHEN NEW.status IN ('active','frozen')
             {balance_guard}
             AND NOT EXISTS (
                SELECT 1 FROM warehouse_locations location
                WHERE location.id = NEW.warehouse_location_id
                  AND location.is_active = 1
                  AND location.placement_status = 'placed'
             )
            BEGIN
                SELECT RAISE(ABORT, 'inventory lot requires an active placed location');
            END
            """.format(
                update_columns=update_columns,
                balance_guard=balance_guard,
            )
        )
    )


def _replace_stocktake_order_update_guard(*, include_layout_version: bool) -> None:
    """Keep the submitted map snapshot immutable across stocktake review."""

    connection = op.get_bind()
    sqlite_layout_guard = (
        "AND NEW.location_layout_version IS OLD.location_layout_version"
        if include_layout_version
        else ""
    )
    postgres_layout_guard = (
        "AND NEW.location_layout_version IS NOT DISTINCT FROM "
        "OLD.location_layout_version"
        if include_layout_version
        else ""
    )
    if connection.dialect.name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_stocktake_orders_update_guard")
        op.execute(
            sa.text(
                """
                CREATE TRIGGER trg_stocktake_orders_update_guard
                BEFORE UPDATE ON stocktake_orders
                WHEN NOT (
                    (OLD.status = 'draft' AND NEW.status = 'draft')
                    OR (
                        OLD.status = 'draft' AND NEW.status = 'submitted'
                        AND NEW.id IS OLD.id
                        AND NEW.order_number IS OLD.order_number
                        AND NEW.location_id IS OLD.location_id
                        {layout_guard}
                        AND NEW.idempotency_key IS OLD.idempotency_key
                        AND NEW.created_at IS OLD.created_at
                        AND NEW.version = OLD.version + 1
                        AND NEW.submitted_by IS NOT NULL
                        AND NEW.submitted_at IS NOT NULL
                        AND NEW.reviewed_by IS NULL
                        AND NEW.reviewed_at IS NULL
                        AND NEW.review_note IS NULL
                    )
                    OR (
                        OLD.status = 'submitted'
                        AND NEW.status IN ('approved', 'rejected')
                        AND NEW.id IS OLD.id
                        AND NEW.order_number IS OLD.order_number
                        AND NEW.location_id IS OLD.location_id
                        {layout_guard}
                        AND NEW.submitted_by IS OLD.submitted_by
                        AND NEW.submitted_at IS OLD.submitted_at
                        AND NEW.idempotency_key IS OLD.idempotency_key
                        AND NEW.created_at IS OLD.created_at
                        AND NEW.version = OLD.version + 1
                        AND NEW.reviewed_by IS NOT NULL
                        AND NEW.reviewed_at IS NOT NULL
                    )
                )
                BEGIN
                    SELECT RAISE(ABORT, 'invalid stocktake order transition or immutable update');
                END
                """.format(layout_guard=sqlite_layout_guard)
            )
        )
    elif connection.dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_stocktake_orders_guard ON stocktake_orders"
        )
        op.execute("DROP FUNCTION IF EXISTS stocktake_orders_guard()")
        op.execute(
            sa.text(
                """
                CREATE FUNCTION stocktake_orders_guard() RETURNS trigger AS $$
                BEGIN
                    IF NOT (
                        (OLD.status = 'draft' AND NEW.status = 'draft')
                        OR (
                            OLD.status = 'draft' AND NEW.status = 'submitted'
                            AND NEW.id IS NOT DISTINCT FROM OLD.id
                            AND NEW.order_number IS NOT DISTINCT FROM OLD.order_number
                            AND NEW.location_id IS NOT DISTINCT FROM OLD.location_id
                            {layout_guard}
                            AND NEW.idempotency_key IS NOT DISTINCT FROM OLD.idempotency_key
                            AND NEW.created_at IS NOT DISTINCT FROM OLD.created_at
                            AND NEW.version = OLD.version + 1
                            AND NEW.submitted_by IS NOT NULL
                            AND NEW.submitted_at IS NOT NULL
                            AND NEW.reviewed_by IS NULL
                            AND NEW.reviewed_at IS NULL
                            AND NEW.review_note IS NULL
                        )
                        OR (
                            OLD.status = 'submitted'
                            AND NEW.status IN ('approved', 'rejected')
                            AND NEW.id IS NOT DISTINCT FROM OLD.id
                            AND NEW.order_number IS NOT DISTINCT FROM OLD.order_number
                            AND NEW.location_id IS NOT DISTINCT FROM OLD.location_id
                            {layout_guard}
                            AND NEW.submitted_by IS NOT DISTINCT FROM OLD.submitted_by
                            AND NEW.submitted_at IS NOT DISTINCT FROM OLD.submitted_at
                            AND NEW.idempotency_key IS NOT DISTINCT FROM OLD.idempotency_key
                            AND NEW.created_at IS NOT DISTINCT FROM OLD.created_at
                            AND NEW.version = OLD.version + 1
                            AND NEW.reviewed_by IS NOT NULL
                            AND NEW.reviewed_at IS NOT NULL
                        )
                    ) THEN
                        RAISE EXCEPTION 'invalid stocktake order transition or immutable update';
                    END IF;
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql;

                CREATE TRIGGER trg_stocktake_orders_guard
                BEFORE UPDATE ON stocktake_orders
                FOR EACH ROW EXECUTE FUNCTION stocktake_orders_guard();
                """.format(layout_guard=postgres_layout_guard)
            )
        )


def upgrade() -> None:
    op.add_column(
        "stocktake_orders",
        sa.Column("location_layout_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "warehouse_location_discrepancies",
        sa.Column(
            "observed_location_layout_version", sa.Integer(), nullable=True
        ),
    )
    _replace_stocktake_order_update_guard(include_layout_version=True)
    with op.batch_alter_table("floor3_location_layouts") as batch_op:
        batch_op.add_column(
            sa.Column(
                "layout_kind",
                sa.String(length=24),
                nullable=False,
                server_default="unknown",
            )
        )
        batch_op.create_check_constraint(
            "ck_floor3_location_layouts_layout_kind",
            "layout_kind IN ('unknown','physical_pallet','logical_anchor')",
        )
    if op.get_bind().dialect.name == "sqlite":
        # Match the application-wide definition of live inventory: historical
        # active/frozen rows whose available, reserved and damaged balances are
        # all zero no longer occupy the physical location.
        _replace_sqlite_unplace_guard(require_positive_balance=True)
        _replace_sqlite_inventory_location_guards(require_positive_balance=True)


def downgrade() -> None:
    connection = op.get_bind()
    used_semantics = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM floor3_location_layouts "
            "WHERE layout_kind <> 'unknown'"
        )
    ).scalar_one()
    if int(used_semantics or 0) > 0:
        raise RuntimeError(
            "拒绝破坏性降级：已有货位使用实体占地或逻辑点位语义"
        )
    used_snapshots = connection.execute(
        sa.text(
            "SELECT "
            "(SELECT COUNT(*) FROM stocktake_orders "
            " WHERE location_layout_version IS NOT NULL) + "
            "(SELECT COUNT(*) FROM warehouse_location_discrepancies "
            " WHERE observed_location_layout_version IS NOT NULL)"
        )
    ).scalar_one()
    if int(used_snapshots or 0) > 0:
        raise RuntimeError(
            "拒绝破坏性降级：已有盘点或位置不符记录使用货位布局版本快照"
        )
    with op.batch_alter_table("floor3_location_layouts") as batch_op:
        batch_op.drop_constraint(
            "ck_floor3_location_layouts_layout_kind",
            type_="check",
        )
        batch_op.drop_column("layout_kind")
    _replace_stocktake_order_update_guard(include_layout_version=False)
    op.drop_column(
        "warehouse_location_discrepancies",
        "observed_location_layout_version",
    )
    op.drop_column("stocktake_orders", "location_layout_version")
    if op.get_bind().dialect.name == "sqlite":
        _replace_sqlite_unplace_guard(require_positive_balance=False)
        _replace_sqlite_inventory_location_guards(require_positive_balance=False)
