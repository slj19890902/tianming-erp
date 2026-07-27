"""N081 full field stocktake posting and pending-location guards.

Revision ID: cn70v8x9z59
Revises: cm69v8x9z58
Create Date: 2026-07-24
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "cn70v8x9z59"
down_revision = "cm69v8x9z58"
branch_labels = None
depends_on = None


POSTING_TABLE = "inventory_onboarding_postings"
LINE_COUNT_CONSTRAINT = "ck_inventory_onboarding_postings_line_count"
SOURCE_LINE_TABLE = "inventory_onboarding_lines"
QUANTITY_CONSTRAINT = "ck_inventory_onboarding_lines_quantity"


def _drop_replaceable_sqlite_guards(connection: sa.Connection) -> None:
    for name in (
        "trg_inventory_onboarding_postings_delete_guard",
        "trg_inventory_onboarding_postings_update_guard",
        "trg_inventory_onboarding_postings_insert_guard",
        "trg_inventory_lots_onboarding_source_insert_guard",
    ):
        connection.execute(sa.text(f"DROP TRIGGER IF EXISTS {name}"))


def _drop_source_line_sqlite_guards(connection: sa.Connection) -> None:
    for name in (
        "trg_inventory_onboarding_lines_delete_guard",
        "trg_inventory_onboarding_lines_update_guard",
        "trg_inventory_onboarding_lines_insert_guard",
    ):
        connection.execute(sa.text(f"DROP TRIGGER IF EXISTS {name}"))


def _create_source_line_sqlite_guards(connection: sa.Connection) -> None:
    connection.execute(
        sa.text(
            """
            CREATE TRIGGER trg_inventory_onboarding_lines_insert_guard
            BEFORE INSERT ON inventory_onboarding_lines
            WHEN NOT EXISTS (
                SELECT 1
                FROM inventory_onboarding_batches
                WHERE id = NEW.batch_id AND status = 'draft'
            )
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'inventory onboarding lines require a draft batch'
                );
            END
            """
        )
    )
    connection.execute(
        sa.text(
            """
            CREATE TRIGGER trg_inventory_onboarding_lines_update_guard
            BEFORE UPDATE ON inventory_onboarding_lines
            WHEN NOT EXISTS (
                    SELECT 1
                    FROM inventory_onboarding_batches
                    WHERE id = OLD.batch_id AND status = 'draft'
                 )
              OR NOT EXISTS (
                    SELECT 1
                    FROM inventory_onboarding_batches
                    WHERE id = NEW.batch_id AND status = 'draft'
                 )
              OR NEW.id IS NOT OLD.id
              OR NEW.batch_id IS NOT OLD.batch_id
              OR NEW.source_sheet_name IS NOT OLD.source_sheet_name
              OR NEW.source_row_number IS NOT OLD.source_row_number
              OR NEW.source_row_hash IS NOT OLD.source_row_hash
              OR NEW.raw_row_text IS NOT OLD.raw_row_text
              OR NEW.original_values_json IS NOT OLD.original_values_json
              OR NEW.created_at IS NOT OLD.created_at
              OR NEW.version <> OLD.version + 1
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'inventory onboarding source row or submitted line is immutable'
                );
            END
            """
        )
    )
    connection.execute(
        sa.text(
            """
            CREATE TRIGGER trg_inventory_onboarding_lines_delete_guard
            BEFORE DELETE ON inventory_onboarding_lines
            WHEN NOT EXISTS (
                SELECT 1
                FROM inventory_onboarding_batches
                WHERE id = OLD.batch_id AND status = 'draft'
            )
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'submitted inventory onboarding line is immutable'
                );
            END
            """
        )
    )


def _create_immutable_receipt_guards(connection: sa.Connection) -> None:
    connection.execute(
        sa.text(
            """
            CREATE TRIGGER trg_inventory_onboarding_postings_update_guard
            BEFORE UPDATE ON inventory_onboarding_postings
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'inventory onboarding posting receipt is immutable'
                );
            END
            """
        )
    )
    connection.execute(
        sa.text(
            """
            CREATE TRIGGER trg_inventory_onboarding_postings_delete_guard
            BEFORE DELETE ON inventory_onboarding_postings
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'inventory onboarding posting receipt is immutable'
                );
            END
            """
        )
    )


def _create_modern_sqlite_guards(connection: sa.Connection) -> None:
    connection.execute(
        sa.text(
            """
            CREATE TRIGGER trg_inventory_lots_onboarding_source_insert_guard
            BEFORE INSERT ON inventory_lots
            WHEN NEW.source_ref_type = 'inventory_onboarding_line'
              AND NOT EXISTS (
                    SELECT 1
                    FROM inventory_onboarding_lines AS source
                    JOIN inventory_onboarding_batches AS batch
                      ON batch.id = source.batch_id
                    JOIN warehouse_locations AS target
                      ON target.id = NEW.warehouse_location_id
                    WHERE source.id = NEW.source_ref_id
                      AND batch.status = 'submitted'
                      AND source.action_decision = 'create_new'
                      AND source.match_status = 'ready'
                      AND source.inventory_type = NEW.inventory_type
                      AND (
                            source.location_id = NEW.warehouse_location_id
                            OR (
                                source.location_id IS NULL
                                AND source.area_code_snapshot = '待定位'
                                AND source.location_code_snapshot LIKE 'PD-%'
                                AND target.location_code
                                      = source.location_code_snapshot
                                AND target.area_code = '待定位'
                                AND target.source_version = 'N081_PENDING'
                                AND target.is_temporary = 1
                            )
                          )
                      AND source.quantity = NEW.quantity_available
                      AND source.unit = NEW.unit
                      AND NEW.source_type = 'stocktake'
                      AND NEW.quantity_available > 0
                      AND NEW.quantity_reserved = 0
                      AND NEW.quantity_consumed = 0
                      AND NEW.quantity_damaged = 0
                      AND NEW.quantity_scrapped = 0
                      AND NEW.status = 'active'
                )
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'onboarding inventory lot requires one eligible source line'
                );
            END
            """
        )
    )
    connection.execute(
        sa.text(
            """
            CREATE TRIGGER trg_inventory_onboarding_postings_insert_guard
            BEFORE INSERT ON inventory_onboarding_postings
            WHEN NOT EXISTS (
                    SELECT 1
                    FROM inventory_onboarding_batches AS batch
                    WHERE batch.id = NEW.onboarding_batch_id
                      AND batch.status = 'submitted'
                      AND batch.version = NEW.onboarding_batch_version
                      AND batch.dry_run_fingerprint
                            = NEW.onboarding_batch_fingerprint
                 )
              OR NEW.line_count <> (
                    SELECT COUNT(*)
                    FROM inventory_onboarding_lines AS source
                    WHERE source.batch_id = NEW.onboarding_batch_id
                      AND (
                            (
                                source.action_decision = 'create_new'
                                AND source.match_status = 'ready'
                            )
                            OR (
                                source.inventory_type = 'finished'
                                AND source.action_decision = 'route_n035'
                                AND source.match_status = 'routed'
                                AND source.existing_lot_id IS NOT NULL
                            )
                          )
                 )
              OR EXISTS (
                    SELECT 1
                    FROM inventory_onboarding_lines AS source
                    WHERE source.batch_id = NEW.onboarding_batch_id
                      AND (
                            (
                                source.action_decision = 'create_new'
                                AND source.match_status = 'ready'
                                AND NOT EXISTS (
                                    SELECT 1
                                    FROM inventory_lots AS lot
                                    WHERE lot.source_ref_type
                                          = 'inventory_onboarding_line'
                                      AND lot.source_ref_id = source.id
                                )
                            )
                            OR (
                                source.inventory_type = 'finished'
                                AND source.action_decision = 'route_n035'
                                AND source.match_status = 'routed'
                                AND source.existing_lot_id IS NOT NULL
                                AND NOT EXISTS (
                                    SELECT 1
                                    FROM inventory_lots AS lot
                                    WHERE lot.id = source.existing_lot_id
                                      AND lot.inventory_type = 'finished'
                                      AND lot.status IN ('active', 'frozen')
                                )
                            )
                          )
                 )
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'posting receipt requires one complete formal batch'
                );
            END
            """
        )
    )
    _create_immutable_receipt_guards(connection)


def _create_legacy_sqlite_guards(connection: sa.Connection) -> None:
    connection.execute(
        sa.text(
            """
            CREATE TRIGGER trg_inventory_lots_onboarding_source_insert_guard
            BEFORE INSERT ON inventory_lots
            WHEN NEW.source_ref_type = 'inventory_onboarding_line'
              AND NOT EXISTS (
                    SELECT 1
                    FROM inventory_onboarding_lines AS source
                    JOIN inventory_onboarding_batches AS batch
                      ON batch.id = source.batch_id
                    WHERE source.id = NEW.source_ref_id
                      AND batch.status = 'submitted'
                      AND source.action_decision = 'create_new'
                      AND source.match_status = 'ready'
                      AND source.inventory_type = NEW.inventory_type
                      AND source.location_id = NEW.warehouse_location_id
                      AND source.quantity = NEW.quantity_available
                      AND source.unit = NEW.unit
                      AND NEW.source_type = 'stocktake'
                      AND NEW.quantity_available > 0
                      AND NEW.quantity_reserved = 0
                      AND NEW.quantity_consumed = 0
                      AND NEW.quantity_damaged = 0
                      AND NEW.quantity_scrapped = 0
                      AND NEW.status = 'active'
                )
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'onboarding inventory lot requires one eligible source line'
                );
            END
            """
        )
    )
    connection.execute(
        sa.text(
            """
            CREATE TRIGGER trg_inventory_onboarding_postings_insert_guard
            BEFORE INSERT ON inventory_onboarding_postings
            WHEN NOT EXISTS (
                    SELECT 1
                    FROM inventory_onboarding_batches AS batch
                    WHERE batch.id = NEW.onboarding_batch_id
                      AND batch.status = 'submitted'
                      AND batch.version = NEW.onboarding_batch_version
                      AND batch.dry_run_fingerprint
                            = NEW.onboarding_batch_fingerprint
                 )
              OR NEW.line_count <> (
                    SELECT COUNT(*)
                    FROM inventory_onboarding_lines AS source
                    WHERE source.batch_id = NEW.onboarding_batch_id
                      AND source.action_decision = 'create_new'
                      AND source.match_status = 'ready'
                 )
              OR NEW.line_count <> (
                    SELECT COUNT(*)
                    FROM inventory_lots AS lot
                    JOIN inventory_onboarding_lines AS source
                      ON source.id = lot.source_ref_id
                    WHERE source.batch_id = NEW.onboarding_batch_id
                      AND lot.source_ref_type = 'inventory_onboarding_line'
                      AND source.action_decision = 'create_new'
                      AND source.match_status = 'ready'
                 )
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'posting receipt requires one complete formal batch'
                );
            END
            """
        )
    )
    _create_immutable_receipt_guards(connection)


def _replace_line_count_constraint(limit: int) -> None:
    with op.batch_alter_table(POSTING_TABLE, recreate="always") as batch:
        batch.drop_constraint(LINE_COUNT_CONSTRAINT, type_="check")
        batch.create_check_constraint(
            LINE_COUNT_CONSTRAINT,
            f"line_count > 0 AND line_count <= {limit}",
        )


def _replace_quantity_constraint(operator: str) -> None:
    with op.batch_alter_table(SOURCE_LINE_TABLE, recreate="always") as batch:
        batch.drop_constraint(QUANTITY_CONSTRAINT, type_="check")
        batch.create_check_constraint(
            QUANTITY_CONSTRAINT,
            f"quantity IS NULL OR quantity {operator} 0",
        )


def _assert_legacy_downgrade_safe(connection: sa.Connection) -> None:
    oversized = connection.execute(
        sa.text(
            """
            SELECT id
            FROM inventory_onboarding_postings
            WHERE line_count > 100
            LIMIT 1
            """
        )
    ).first()
    if oversized is not None:
        raise RuntimeError(
            "cannot downgrade while a posting contains more than 100 lines"
        )
    zero_count = connection.execute(
        sa.text(
            """
            SELECT id
            FROM inventory_onboarding_lines
            WHERE quantity = 0
            LIMIT 1
            """
        )
    ).first()
    if zero_count is not None:
        raise RuntimeError(
            "cannot downgrade while a zero-count stocktake line exists"
        )
    modern_fact = connection.execute(
        sa.text(
            """
            SELECT posting.id
            FROM inventory_onboarding_postings AS posting
            JOIN inventory_onboarding_lines AS source
              ON source.batch_id = posting.onboarding_batch_id
            WHERE (
                    source.action_decision = 'route_n035'
                    AND source.match_status = 'routed'
                  )
               OR (
                    source.location_id IS NULL
                    AND source.area_code_snapshot = '待定位'
                    AND source.location_code_snapshot LIKE 'PD-%'
                  )
            LIMIT 1
            """
        )
    ).first()
    if modern_fact is not None:
        raise RuntimeError(
            "cannot downgrade while full-field stocktake posting facts exist"
        )


def upgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        _drop_replaceable_sqlite_guards(connection)
        _drop_source_line_sqlite_guards(connection)
    _replace_quantity_constraint(">=")
    if connection.dialect.name == "sqlite":
        _create_source_line_sqlite_guards(connection)
    _replace_line_count_constraint(500)
    if connection.dialect.name == "sqlite":
        _create_modern_sqlite_guards(connection)


def downgrade() -> None:
    connection = op.get_bind()
    _assert_legacy_downgrade_safe(connection)
    if connection.dialect.name == "sqlite":
        _drop_replaceable_sqlite_guards(connection)
        _drop_source_line_sqlite_guards(connection)
    _replace_quantity_constraint(">")
    if connection.dialect.name == "sqlite":
        _create_source_line_sqlite_guards(connection)
    _replace_line_count_constraint(100)
    if connection.dialect.name == "sqlite":
        _create_legacy_sqlite_guards(connection)
