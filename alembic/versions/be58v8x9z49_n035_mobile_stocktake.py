"""N035: mobile stocktake snapshots and immutable review history.

Revision ID: be58v8x9z49
Revises: bd57v8x9z47

This revision follows N034 linearly from ``bd57v8x9z47`` so the formal
migration chain retains a single head.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "be58v8x9z49"
down_revision: Union[str, Sequence[str], None] = "bd57v8x9z47"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ORDER_TABLE = "stocktake_orders"
ITEM_TABLE = "stocktake_items"
REVIEW_TABLE = "stocktake_reviews"


def _create_sqlite_guards(connection: sa.Connection) -> None:
    connection.execute(
        sa.text(
            """
            CREATE TRIGGER trg_stocktake_orders_insert_guard
            BEFORE INSERT ON stocktake_orders
            WHEN NEW.status <> 'draft'
            BEGIN
                SELECT RAISE(ABORT, 'stocktake order must be created as draft');
            END
            """
        )
    )
    connection.execute(
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
            """
        )
    )
    connection.execute(
        sa.text(
            """
            CREATE TRIGGER trg_stocktake_orders_delete_guard
            BEFORE DELETE ON stocktake_orders
            WHEN OLD.status <> 'draft'
            BEGIN
                SELECT RAISE(ABORT, 'submitted or reviewed stocktake order is immutable');
            END
            """
        )
    )
    connection.execute(
        sa.text(
            """
            CREATE TRIGGER trg_stocktake_items_insert_guard
            BEFORE INSERT ON stocktake_items
            WHEN NOT EXISTS (
                SELECT 1 FROM stocktake_orders
                WHERE id = NEW.order_id AND status = 'draft'
            )
            BEGIN
                SELECT RAISE(ABORT, 'stocktake items may only be inserted into a draft order');
            END
            """
        )
    )
    connection.execute(
        sa.text(
            """
            CREATE TRIGGER trg_stocktake_items_update_guard
            BEFORE UPDATE ON stocktake_items
            WHEN NOT (
                (
                    EXISTS (
                        SELECT 1 FROM stocktake_orders
                        WHERE id = OLD.order_id AND status = 'draft'
                    )
                    AND EXISTS (
                        SELECT 1 FROM stocktake_orders
                        WHERE id = NEW.order_id AND status = 'draft'
                    )
                )
                OR (
                    EXISTS (
                        SELECT 1 FROM stocktake_orders
                        WHERE id = OLD.order_id AND status = 'approved'
                    )
                    AND OLD.adjustment_movement_id IS NULL
                    AND NEW.adjustment_movement_id IS NOT NULL
                    AND NEW.id IS OLD.id
                    AND NEW.order_id IS OLD.order_id
                    AND NEW.inventory_lot_id IS OLD.inventory_lot_id
                    AND NEW.lot_version_snapshot IS OLD.lot_version_snapshot
                    AND NEW.available_quantity_snapshot IS OLD.available_quantity_snapshot
                    AND NEW.reserved_quantity_snapshot IS OLD.reserved_quantity_snapshot
                    AND NEW.on_hand_quantity_snapshot IS OLD.on_hand_quantity_snapshot
                    AND NEW.counted_quantity IS OLD.counted_quantity
                    AND NEW.difference_quantity IS OLD.difference_quantity
                    AND NEW.lot_number_snapshot IS OLD.lot_number_snapshot
                    AND NEW.customer_name_snapshot IS OLD.customer_name_snapshot
                    AND NEW.product_name_snapshot IS OLD.product_name_snapshot
                    AND NEW.specification_snapshot IS OLD.specification_snapshot
                    AND NEW.inventory_code_snapshot IS OLD.inventory_code_snapshot
                    AND NEW.location_code_snapshot IS OLD.location_code_snapshot
                    AND NEW.unit_snapshot IS OLD.unit_snapshot
                    AND NEW.created_at IS OLD.created_at
                    AND EXISTS (
                        SELECT 1 FROM inventory_movements
                        WHERE id = NEW.adjustment_movement_id
                          AND inventory_lot_id = OLD.inventory_lot_id
                          AND movement_type = 'adjust'
                    )
                )
            )
            BEGIN
                SELECT RAISE(ABORT, 'stocktake item is immutable or adjustment movement is invalid');
            END
            """
        )
    )
    connection.execute(
        sa.text(
            """
            CREATE TRIGGER trg_stocktake_items_delete_guard
            BEFORE DELETE ON stocktake_items
            WHEN NOT EXISTS (
                SELECT 1 FROM stocktake_orders
                WHERE id = OLD.order_id AND status = 'draft'
            )
            BEGIN
                SELECT RAISE(ABORT, 'submitted or reviewed stocktake item is immutable');
            END
            """
        )
    )
    for event in ("UPDATE", "DELETE"):
        connection.execute(
            sa.text(
                f"""
                CREATE TRIGGER trg_stocktake_reviews_immutable_{event.lower()}
                BEFORE {event} ON stocktake_reviews
                BEGIN
                    SELECT RAISE(ABORT, 'stocktake review history is immutable');
                END
                """
            )
        )


def _create_postgresql_guards(connection: sa.Connection) -> None:
    connection.execute(
        sa.text(
            """
            CREATE FUNCTION stocktake_orders_insert_guard() RETURNS trigger AS $$
            BEGIN
                IF NEW.status <> 'draft' THEN
                    RAISE EXCEPTION 'stocktake order must be created as draft';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;

            CREATE FUNCTION stocktake_orders_guard() RETURNS trigger AS $$
            BEGIN
                IF NOT (
                    (OLD.status = 'draft' AND NEW.status = 'draft')
                    OR (
                        OLD.status = 'draft' AND NEW.status = 'submitted'
                        AND NEW.id IS NOT DISTINCT FROM OLD.id
                        AND NEW.order_number IS NOT DISTINCT FROM OLD.order_number
                        AND NEW.location_id IS NOT DISTINCT FROM OLD.location_id
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

            CREATE FUNCTION stocktake_orders_delete_guard() RETURNS trigger AS $$
            BEGIN
                IF OLD.status <> 'draft' THEN
                    RAISE EXCEPTION 'submitted or reviewed stocktake order is immutable';
                END IF;
                RETURN OLD;
            END;
            $$ LANGUAGE plpgsql;

            CREATE FUNCTION stocktake_items_insert_guard() RETURNS trigger AS $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM stocktake_orders
                    WHERE id = NEW.order_id AND status = 'draft'
                ) THEN
                    RAISE EXCEPTION 'stocktake items may only be inserted into a draft order';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;

            CREATE FUNCTION stocktake_items_guard() RETURNS trigger AS $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM stocktake_orders
                    WHERE id = OLD.order_id AND status = 'draft'
                ) AND EXISTS (
                    SELECT 1 FROM stocktake_orders
                    WHERE id = NEW.order_id AND status = 'draft'
                ) THEN
                    RETURN NEW;
                END IF;
                IF EXISTS (
                    SELECT 1 FROM stocktake_orders
                    WHERE id = OLD.order_id AND status = 'approved'
                )
                    AND OLD.adjustment_movement_id IS NULL
                    AND NEW.adjustment_movement_id IS NOT NULL
                    AND NEW.id IS NOT DISTINCT FROM OLD.id
                    AND NEW.order_id IS NOT DISTINCT FROM OLD.order_id
                    AND NEW.inventory_lot_id IS NOT DISTINCT FROM OLD.inventory_lot_id
                    AND NEW.lot_version_snapshot IS NOT DISTINCT FROM OLD.lot_version_snapshot
                    AND NEW.available_quantity_snapshot IS NOT DISTINCT FROM OLD.available_quantity_snapshot
                    AND NEW.reserved_quantity_snapshot IS NOT DISTINCT FROM OLD.reserved_quantity_snapshot
                    AND NEW.on_hand_quantity_snapshot IS NOT DISTINCT FROM OLD.on_hand_quantity_snapshot
                    AND NEW.counted_quantity IS NOT DISTINCT FROM OLD.counted_quantity
                    AND NEW.difference_quantity IS NOT DISTINCT FROM OLD.difference_quantity
                    AND NEW.lot_number_snapshot IS NOT DISTINCT FROM OLD.lot_number_snapshot
                    AND NEW.customer_name_snapshot IS NOT DISTINCT FROM OLD.customer_name_snapshot
                    AND NEW.product_name_snapshot IS NOT DISTINCT FROM OLD.product_name_snapshot
                    AND NEW.specification_snapshot IS NOT DISTINCT FROM OLD.specification_snapshot
                    AND NEW.inventory_code_snapshot IS NOT DISTINCT FROM OLD.inventory_code_snapshot
                    AND NEW.location_code_snapshot IS NOT DISTINCT FROM OLD.location_code_snapshot
                    AND NEW.unit_snapshot IS NOT DISTINCT FROM OLD.unit_snapshot
                    AND NEW.created_at IS NOT DISTINCT FROM OLD.created_at
                    AND EXISTS (
                        SELECT 1 FROM inventory_movements
                        WHERE id = NEW.adjustment_movement_id
                          AND inventory_lot_id = OLD.inventory_lot_id
                          AND movement_type = 'adjust'
                    ) THEN
                    RETURN NEW;
                END IF;
                RAISE EXCEPTION 'stocktake item is immutable or adjustment movement is invalid';
            END;
            $$ LANGUAGE plpgsql;

            CREATE FUNCTION stocktake_items_delete_guard() RETURNS trigger AS $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM stocktake_orders
                    WHERE id = OLD.order_id AND status = 'draft'
                ) THEN
                    RAISE EXCEPTION 'submitted or reviewed stocktake item is immutable';
                END IF;
                RETURN OLD;
            END;
            $$ LANGUAGE plpgsql;

            CREATE FUNCTION stocktake_reviews_guard() RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'stocktake review history is immutable';
            END;
            $$ LANGUAGE plpgsql;

            CREATE TRIGGER trg_stocktake_orders_insert_guard
            BEFORE INSERT ON stocktake_orders
            FOR EACH ROW EXECUTE FUNCTION stocktake_orders_insert_guard();
            CREATE TRIGGER trg_stocktake_orders_guard
            BEFORE UPDATE ON stocktake_orders
            FOR EACH ROW EXECUTE FUNCTION stocktake_orders_guard();
            CREATE TRIGGER trg_stocktake_orders_delete_guard
            BEFORE DELETE ON stocktake_orders
            FOR EACH ROW EXECUTE FUNCTION stocktake_orders_delete_guard();
            CREATE TRIGGER trg_stocktake_items_insert_guard
            BEFORE INSERT ON stocktake_items
            FOR EACH ROW EXECUTE FUNCTION stocktake_items_insert_guard();
            CREATE TRIGGER trg_stocktake_items_update_guard
            BEFORE UPDATE ON stocktake_items
            FOR EACH ROW EXECUTE FUNCTION stocktake_items_guard();
            CREATE TRIGGER trg_stocktake_items_delete_guard
            BEFORE DELETE ON stocktake_items
            FOR EACH ROW EXECUTE FUNCTION stocktake_items_delete_guard();
            CREATE TRIGGER trg_stocktake_reviews_immutable_update
            BEFORE UPDATE ON stocktake_reviews
            FOR EACH ROW EXECUTE FUNCTION stocktake_reviews_guard();
            CREATE TRIGGER trg_stocktake_reviews_immutable_delete
            BEFORE DELETE ON stocktake_reviews
            FOR EACH ROW EXECUTE FUNCTION stocktake_reviews_guard();
            """
        )
    )


def _create_guards(connection: sa.Connection) -> None:
    if connection.dialect.name == "sqlite":
        _create_sqlite_guards(connection)
    elif connection.dialect.name == "postgresql":
        _create_postgresql_guards(connection)


def _drop_guards(connection: sa.Connection) -> None:
    if connection.dialect.name == "sqlite":
        for name in (
            "trg_stocktake_reviews_immutable_delete",
            "trg_stocktake_reviews_immutable_update",
            "trg_stocktake_items_delete_guard",
            "trg_stocktake_items_update_guard",
            "trg_stocktake_items_insert_guard",
            "trg_stocktake_orders_delete_guard",
            "trg_stocktake_orders_update_guard",
            "trg_stocktake_orders_insert_guard",
        ):
            connection.execute(sa.text(f"DROP TRIGGER IF EXISTS {name}"))
    elif connection.dialect.name == "postgresql":
        for table, name in (
            (REVIEW_TABLE, "trg_stocktake_reviews_immutable_delete"),
            (REVIEW_TABLE, "trg_stocktake_reviews_immutable_update"),
            (ITEM_TABLE, "trg_stocktake_items_delete_guard"),
            (ITEM_TABLE, "trg_stocktake_items_update_guard"),
            (ITEM_TABLE, "trg_stocktake_items_insert_guard"),
            (ORDER_TABLE, "trg_stocktake_orders_delete_guard"),
            (ORDER_TABLE, "trg_stocktake_orders_guard"),
            (ORDER_TABLE, "trg_stocktake_orders_insert_guard"),
        ):
            connection.execute(sa.text(f"DROP TRIGGER IF EXISTS {name} ON {table}"))
        for name in (
            "stocktake_reviews_guard()",
            "stocktake_items_delete_guard()",
            "stocktake_items_guard()",
            "stocktake_items_insert_guard()",
            "stocktake_orders_delete_guard()",
            "stocktake_orders_guard()",
            "stocktake_orders_insert_guard()",
        ):
            connection.execute(sa.text(f"DROP FUNCTION IF EXISTS {name}"))


def upgrade() -> None:
    op.create_table(
        ORDER_TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("order_number", sa.String(length=50), nullable=False),
        sa.Column("location_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="draft"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("submitted_by", sa.Integer(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("reviewed_by", sa.Integer(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("status IN ('draft','submitted','approved','rejected')", name="ck_stocktake_orders_status"),
        sa.CheckConstraint("version >= 1", name="ck_stocktake_orders_version"),
        sa.CheckConstraint(
            "(status IN ('draft', 'submitted') AND reviewed_by IS NULL "
            "AND reviewed_at IS NULL AND review_note IS NULL) OR "
            "(status IN ('approved', 'rejected') AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)",
            name="ck_stocktake_orders_review_state",
        ),
        sa.ForeignKeyConstraint(["location_id"], ["warehouse_locations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["submitted_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["reviewed_by"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("order_number", name="uq_stocktake_orders_number"),
        sa.UniqueConstraint("idempotency_key", name="uq_stocktake_orders_idempotency"),
    )
    op.create_index("ix_stocktake_orders_location_status", ORDER_TABLE, ["location_id", "status"])

    op.create_table(
        ITEM_TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("inventory_lot_id", sa.Integer(), nullable=False),
        sa.Column("lot_version_snapshot", sa.Integer(), nullable=False),
        sa.Column("available_quantity_snapshot", sa.Integer(), nullable=False),
        sa.Column("reserved_quantity_snapshot", sa.Integer(), nullable=False),
        sa.Column("on_hand_quantity_snapshot", sa.Integer(), nullable=False),
        sa.Column("counted_quantity", sa.Integer(), nullable=False),
        sa.Column("difference_quantity", sa.Integer(), nullable=False),
        sa.Column("lot_number_snapshot", sa.String(length=50), nullable=False),
        sa.Column("customer_name_snapshot", sa.String(length=200), nullable=True),
        sa.Column("product_name_snapshot", sa.String(length=250), nullable=True),
        sa.Column("specification_snapshot", sa.String(length=150), nullable=True),
        sa.Column("inventory_code_snapshot", sa.String(length=150), nullable=True),
        sa.Column("location_code_snapshot", sa.String(length=50), nullable=False),
        sa.Column("unit_snapshot", sa.String(length=20), nullable=False),
        sa.Column("adjustment_movement_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("lot_version_snapshot >= 1", name="ck_stocktake_items_lot_version"),
        sa.CheckConstraint(
            "available_quantity_snapshot >= 0 AND reserved_quantity_snapshot >= 0 "
            "AND on_hand_quantity_snapshot >= 0 AND counted_quantity >= 0",
            name="ck_stocktake_items_quantities_nonnegative",
        ),
        sa.CheckConstraint(
            "on_hand_quantity_snapshot = available_quantity_snapshot + reserved_quantity_snapshot",
            name="ck_stocktake_items_on_hand_snapshot",
        ),
        sa.CheckConstraint(
            "difference_quantity = counted_quantity - on_hand_quantity_snapshot",
            name="ck_stocktake_items_difference",
        ),
        sa.ForeignKeyConstraint(["order_id"], ["stocktake_orders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["inventory_lot_id"], ["inventory_lots.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["adjustment_movement_id"], ["inventory_movements.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("order_id", "inventory_lot_id", name="uq_stocktake_items_order_lot"),
    )
    op.create_index("ix_stocktake_items_inventory_lot", ITEM_TABLE, ["inventory_lot_id"])
    op.create_index("ix_stocktake_items_adjustment_movement", ITEM_TABLE, ["adjustment_movement_id"])

    op.create_table(
        REVIEW_TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("from_status", sa.String(length=20), nullable=False),
        sa.Column("to_status", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("details_json", sa.JSON(), nullable=True),
        sa.Column("reviewed_by", sa.Integer(), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("sequence >= 1", name="ck_stocktake_reviews_sequence"),
        sa.CheckConstraint("action IN ('approve','reject')", name="ck_stocktake_reviews_action"),
        sa.CheckConstraint(
            "(action = 'approve' AND from_status = 'submitted' AND to_status = 'approved') OR "
            "(action = 'reject' AND from_status = 'submitted' AND to_status = 'rejected')",
            name="ck_stocktake_reviews_transition",
        ),
        sa.ForeignKeyConstraint(["order_id"], ["stocktake_orders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reviewed_by"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("order_id", "sequence", name="uq_stocktake_reviews_order_sequence"),
        sa.UniqueConstraint("idempotency_key", name="uq_stocktake_reviews_idempotency"),
    )
    op.create_index("ix_stocktake_reviews_order_reviewed", REVIEW_TABLE, ["order_id", "reviewed_at"])
    _create_guards(op.get_bind())


def _assert_stocktake_tables_empty(connection: sa.Connection) -> None:
    populated = []
    for table_name in (ORDER_TABLE, ITEM_TABLE, REVIEW_TABLE):
        row_count = connection.execute(
            sa.text(f"SELECT COUNT(*) FROM {table_name}")
        ).scalar_one()
        if row_count:
            populated.append(f"{table_name}={row_count}")
    if populated:
        raise RuntimeError(
            "N035 downgrade is fail-closed: stocktake business rows exist ("
            + ", ".join(populated)
            + "); restore a backup instead of silently discarding them."
        )


def downgrade() -> None:
    connection = op.get_bind()
    _assert_stocktake_tables_empty(connection)
    _drop_guards(connection)
    op.drop_index("ix_stocktake_reviews_order_reviewed", table_name=REVIEW_TABLE)
    op.drop_table(REVIEW_TABLE)
    op.drop_index("ix_stocktake_items_adjustment_movement", table_name=ITEM_TABLE)
    op.drop_index("ix_stocktake_items_inventory_lot", table_name=ITEM_TABLE)
    op.drop_table(ITEM_TABLE)
    op.drop_index("ix_stocktake_orders_location_status", table_name=ORDER_TABLE)
    op.drop_table(ORDER_TABLE)
