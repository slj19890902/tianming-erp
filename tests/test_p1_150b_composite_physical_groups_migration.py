from __future__ import annotations

import importlib.util
import io
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory


ROOT = Path(__file__).resolve().parents[1]
PARENT = "jp74v8x9z63"
TARGET = "jq75v8x9z64"
GROUP_TABLE = "composite_physical_purchase_groups"
SOURCE_TABLE = "composite_physical_purchase_group_sources"
RECEIPT_TABLE = "composite_physical_group_receipts"
ALLOCATION_TABLE = "composite_physical_group_receipt_source_allocations"
REVERSAL_TABLE = "composite_physical_group_receipt_reversals"
NEW_TABLES = {
    GROUP_TABLE,
    SOURCE_TABLE,
    RECEIPT_TABLE,
    ALLOCATION_TABLE,
    REVERSAL_TABLE,
}
INCOMING_TRIGGER = "trg_p1_150b_test_incoming_receipt"
COST_TRIGGER = "trg_finance_delivery_material_cost_facts_immutable_update"
MIGRATION_FILE = (
    ROOT
    / "alembic"
    / "versions"
    / "jq75v8x9z64_p1_150b_composite_physical_groups.py"
)


def test_p1_150b_migration_is_self_contained() -> None:
    source = MIGRATION_FILE.read_text(encoding="utf-8")

    assert "from app.models" not in source
    assert "Base.metadata" not in source
    assert source.count("op.create_table(") == len(NEW_TABLES)


def _load_migration_module():
    spec = importlib.util.spec_from_file_location("p1_150b_migration", MIGRATION_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_p1_150b_postgresql_offline_ddl_uses_native_boolean_predicates() -> None:
    migration = _load_migration_module()
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    migration.op = Operations(context)
    migration.upgrade()
    sql = output.getvalue()

    assert "is_die_cut_snapshot IS TRUE" in sql
    assert "is_die_cut_snapshot IS FALSE" in sql
    assert "is_last_source IS TRUE" in sql
    assert "is_last_source IS FALSE" in sql
    assert "is_die_cut_snapshot = 1" not in sql
    assert "is_die_cut_snapshot = 0" not in sql
    assert "is_last_source = 1" not in sql
    assert "is_last_source = 0" not in sql


def _config(monkeypatch: pytest.MonkeyPatch, database: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(database.parent / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "p1-150b-physical-group-migration-test")
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return config


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def _columns(connection: sqlite3.Connection, table: str) -> dict[str, int]:
    return {
        str(row[1]): int(row[3])
        for row in connection.execute(f"PRAGMA table_info({table})")
    }


def _triggers(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        )
    }


def _health(connection: sqlite3.Connection, revision: str) -> None:
    connection.execute("PRAGMA foreign_keys=ON")
    assert connection.execute(
        "SELECT version_num FROM alembic_version"
    ).fetchone() == (revision,)
    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _insert_group(connection: sqlite3.Connection, group_id: int = 1) -> None:
    connection.execute(
        f"""
        INSERT INTO {GROUP_TABLE}(
            id, group_key, requisition_id, customer_id, component_product_id,
            material_id, material_version_snapshot, material_code_snapshot,
            layer_count_snapshot, flute_type_snapshot,
            report_length_mm, report_width_mm, sheet_type_snapshot,
            cutting_mode_snapshot, yield_per_sheet, is_die_cut_snapshot,
            source_count, total_required_piece_quantity,
            total_inventory_reserved_piece_quantity,
            net_required_piece_quantity, spare_sheet_quantity,
            order_purpose_sheet_quantity, reserve_sheet_quantity,
            purchase_sheet_quantity, cutting_remainder_piece_quantity,
            physical_snapshot_hash, status, version,
            idempotency_key, request_hash, created_by
        ) VALUES (
            ?, ?, 100, 200, 300,
            400, 2, 'K=A', 5, 'BC',
            1200, 800, 'raw_board', '一开四', 4, 0,
            2, 6, 1, 5, 1, 3, 2, 5, 3,
            ?, 'active', 1, ?, ?, 900
        )
        """,
        (
            group_id,
            f"CPG-{group_id}",
            f"{group_id:064x}",
            f"create-{group_id}",
            f"{group_id + 10:064x}",
        ),
    )


def _insert_source(
    connection: sqlite3.Connection,
    *,
    source_id: int,
    sequence: int,
    before: int,
    allocated: int,
    spare: int,
) -> None:
    is_last = int(sequence == 2)
    connection.execute(
        f"""
        INSERT INTO {SOURCE_TABLE}(
            id, composite_physical_purchase_group_id, source_key,
            requisition_item_id, requisition_item_bom_source_id,
            order_item_id, sales_order_item_bom_component_id,
            source_sequence, source_count_snapshot, is_last_source,
            demand_basis, component_type_snapshot,
            parent_set_quantity, quantity_per_set,
            required_piece_quantity, inventory_reserved_piece_quantity,
            net_required_piece_quantity, spare_sheet_quantity,
            allocated_order_purpose_sheet_quantity,
            cumulative_allocated_sheet_quantity_before,
            cumulative_allocated_sheet_quantity_after,
            group_order_purpose_sheet_quantity_snapshot,
            source_fingerprint, created_by
        ) VALUES (
            ?, 1, ?, ?, ?, ?, ?,
            ?, 2, ?, 'order_sets', ?,
            1, 3, 3, ?, ?, ?, ?, ?, ?, 3, ?, 900
        )
        """,
        (
            source_id,
            f"SRC-{source_id}",
            1000 + source_id,
            2000 + source_id,
            3000 + source_id,
            4000 + source_id,
            sequence,
            is_last,
            "cover" if sequence == 1 else "base",
            1 if sequence == 1 else 0,
            2 if sequence == 1 else 3,
            spare,
            allocated,
            before,
            before + allocated,
            f"{source_id + 20:064x}",
        ),
    )


def _insert_group_incoming(connection: sqlite3.Connection, item_id: int) -> None:
    connection.execute(
        """
        INSERT INTO incoming_receipt_items(
            id, receipt_id, composite_physical_purchase_group_id,
            planned_quantity, received_quantity,
            cumulative_received_quantity, variance_quantity,
            variance_type, resolution_status, status
        ) VALUES (?, ?, 1, 5, 3, 3, -2, 'short', 'pending', 'posted')
        """,
        (item_id, 5000 + item_id),
    )


def _insert_receipt(
    connection: sqlite3.Connection,
    *,
    receipt_id: int,
    incoming_id: int,
    sequence: int,
    order_sheets: int,
    reserve_sheets: int,
    component_lot: int | None,
    reserve_lot: int | None,
) -> None:
    received = order_sheets + reserve_sheets
    output = order_sheets * 4
    unit_price = 2.5
    connection.execute(
        f"""
        INSERT INTO {RECEIPT_TABLE}(
            id, composite_physical_purchase_group_id,
            incoming_receipt_item_id, receipt_sequence,
            purchase_receipt_fact_id, component_inventory_lot_id,
            reserve_inventory_lot_id, group_version_snapshot,
            received_sheet_quantity,
            order_purpose_received_sheet_quantity,
            reserve_received_sheet_quantity,
            cumulative_received_sheet_quantity_before,
            cumulative_received_sheet_quantity_after,
            cumulative_order_purpose_sheet_quantity_before,
            cumulative_order_purpose_sheet_quantity_after,
            cumulative_reserve_sheet_quantity_before,
            cumulative_reserve_sheet_quantity_after,
            yield_per_sheet_snapshot, component_output_piece_quantity,
            cumulative_component_output_piece_quantity_before,
            cumulative_component_output_piece_quantity_after,
            actual_unit_price_per_sheet, component_unit_material_cost,
            order_purpose_material_cost, reserve_material_cost,
            total_material_cost, currency_snapshot, status, version,
            idempotency_key, request_hash, created_by
        ) VALUES (
            ?, 1, ?, ?, 6000, ?, ?, 1,
            ?, ?, ?, 0, ?, 0, ?, 0, ?,
            4, ?, 0, ?, 2.5, ?, ?, ?, ?,
            'CNY', 'posted', 1, ?, ?, 900
        )
        """,
        (
            receipt_id,
            incoming_id,
            sequence,
            component_lot,
            reserve_lot,
            received,
            order_sheets,
            reserve_sheets,
            received,
            order_sheets,
            reserve_sheets,
            output,
            output,
            None if output == 0 else unit_price / 4,
            unit_price * order_sheets,
            unit_price * reserve_sheets,
            unit_price * received,
            f"receipt-{receipt_id}",
            f"{receipt_id + 30:064x}",
        ),
    )


def test_p1_150b_migration_is_linear_empty_and_round_trips(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "p1-150b-round-trip.sqlite3"
    config = _config(monkeypatch, database)
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == [TARGET]
    assert script.get_revision(TARGET).down_revision == PARENT

    command.upgrade(config, PARENT)
    with sqlite3.connect(database) as connection:
        _health(connection, PARENT)
        connection.execute(
            f"""
            CREATE TRIGGER {INCOMING_TRIGGER}
            AFTER UPDATE ON incoming_receipt_items
            FOR EACH ROW BEGIN SELECT 1; END
            """
        )
        connection.commit()
        assert "composite_physical_purchase_group_id" not in _columns(
            connection, "incoming_receipt_items"
        )
        assert _columns(connection, "finance_delivery_material_cost_facts")[
            "incoming_receipt_purpose_allocation_id"
        ] == 1

    command.upgrade(config, TARGET)
    with sqlite3.connect(database) as connection:
        _health(connection, TARGET)
        assert NEW_TABLES <= _tables(connection)
        assert all(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
            for table in NEW_TABLES
        )
        assert "composite_physical_purchase_group_id" in _columns(
            connection, "incoming_receipt_items"
        )
        cost_columns = _columns(
            connection, "finance_delivery_material_cost_facts"
        )
        assert cost_columns["incoming_receipt_purpose_allocation_id"] == 0
        assert cost_columns["composite_physical_group_receipt_id"] == 0
        assert {INCOMING_TRIGGER, COST_TRIGGER} <= _triggers(connection)

    command.downgrade(config, PARENT)
    with sqlite3.connect(database) as connection:
        _health(connection, PARENT)
        assert not (NEW_TABLES & _tables(connection))
        assert "composite_physical_purchase_group_id" not in _columns(
            connection, "incoming_receipt_items"
        )
        cost_columns = _columns(
            connection, "finance_delivery_material_cost_facts"
        )
        assert cost_columns["incoming_receipt_purpose_allocation_id"] == 1
        assert "composite_physical_group_receipt_id" not in cost_columns
        assert {INCOMING_TRIGGER, COST_TRIGGER} <= _triggers(connection)

    command.upgrade(config, TARGET)
    with sqlite3.connect(database) as connection:
        _health(connection, TARGET)


def test_p1_150b_constraints_cover_group_receipt_cost_and_reversal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "p1-150b-constraints.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, TARGET)

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        _insert_group(connection)
        _insert_source(
            connection, source_id=1, sequence=1, before=0, allocated=1, spare=0
        )
        _insert_source(
            connection, source_id=2, sequence=2, before=1, allocated=2, spare=1
        )
        _insert_group_incoming(connection, 1)
        _insert_receipt(
            connection,
            receipt_id=1,
            incoming_id=1,
            sequence=1,
            order_sheets=2,
            reserve_sheets=1,
            component_lot=7001,
            reserve_lot=7002,
        )
        connection.execute(
            f"""
            INSERT INTO {ALLOCATION_TABLE}(
                id, composite_physical_group_receipt_id,
                composite_physical_purchase_group_source_id,
                allocation_sequence, order_id, order_item_id,
                sales_order_item_bom_component_id,
                inventory_reservation_id,
                source_net_required_piece_quantity_snapshot,
                allocated_reserved_component_piece_quantity,
                source_cumulative_reserved_piece_quantity_before,
                source_cumulative_reserved_piece_quantity_after,
                status, version, idempotency_key, request_hash, created_by
            ) VALUES (
                1, 1, 1, 1, 8001, 8002, 8003, 8004,
                2, 2, 0, 2, 'active', 1, 'allocation-1', ?, 900
            )
            """,
            ("a" * 64,),
        )
        connection.execute(
            f"""
            INSERT INTO {REVERSAL_TABLE}(
                id, composite_physical_group_receipt_id,
                receipt_version_before, receipt_version_after,
                reversed_sheet_quantity,
                reversed_component_output_piece_quantity,
                reversed_reserved_component_piece_quantity,
                reason, idempotency_key, request_hash, reversed_by
            ) VALUES (
                1, 1, 1, 2, 3, 8, 2,
                'system group receipt reversal', 'reversal-1', ?, 900
            )
            """,
            ("b" * 64,),
        )
        connection.execute(
            """
            INSERT INTO finance_delivery_material_cost_facts(
                source_kind, snapshot_version, delivery_id, delivery_item_id,
                delivery_inventory_allocation_id, inventory_lot_id,
                incoming_receipt_purpose_allocation_id,
                composite_physical_group_receipt_id,
                purchase_receipt_fact_id, consumed_quantity,
                quantity_unit_snapshot, unit_material_cost,
                total_material_cost, currency_snapshot,
                tax_included_snapshot, tax_rate_snapshot, source_fingerprint
            ) VALUES (
                'inventory_allocation', 1, 9001, 9002, 9003, 7001,
                NULL, 1, 6000, 2, 'pieces', 0.625, 1.25,
                'CNY', 1, 0.13, ?
            )
            """,
            ("c" * 64,),
        )

        # A second partial receipt may reuse the same frozen price fact and may
        # be reserve-only without manufacturing a virtual component lot.
        _insert_group_incoming(connection, 2)
        _insert_receipt(
            connection,
            receipt_id=2,
            incoming_id=2,
            sequence=2,
            order_sheets=0,
            reserve_sheets=1,
            component_lot=None,
            reserve_lot=7003,
        )
        connection.commit()

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO incoming_receipt_items(
                    receipt_id, order_id, order_item_id,
                    composite_physical_purchase_group_id,
                    planned_quantity, received_quantity,
                    cumulative_received_quantity, variance_quantity,
                    variance_type, resolution_status, status
                ) VALUES (
                    999, 1, 1, 1, 1, 1, 1, 0,
                    'matched', 'not_required', 'posted'
                )
                """
            )
        connection.rollback()

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"""
                INSERT INTO {ALLOCATION_TABLE}(
                    composite_physical_group_receipt_id,
                    composite_physical_purchase_group_source_id,
                    allocation_sequence, order_id, order_item_id,
                    sales_order_item_bom_component_id,
                    inventory_reservation_id,
                    source_net_required_piece_quantity_snapshot,
                    allocated_reserved_component_piece_quantity,
                    source_cumulative_reserved_piece_quantity_before,
                    source_cumulative_reserved_piece_quantity_after,
                    status, version, idempotency_key, request_hash, created_by
                ) VALUES (
                    2, 2, 1, 1, 2, 3, 4,
                    3, 2, 2, 4,
                    'active', 1, 'allocation-over-net', ?, 900
                )
                """,
                ("d" * 64,),
            )
        connection.rollback()

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO finance_delivery_material_cost_facts(
                    source_kind, snapshot_version, delivery_id, delivery_item_id,
                    delivery_inventory_allocation_id, inventory_lot_id,
                    incoming_receipt_purpose_allocation_id,
                    composite_physical_group_receipt_id,
                    purchase_receipt_fact_id, consumed_quantity,
                    quantity_unit_snapshot, unit_material_cost,
                    total_material_cost, currency_snapshot,
                    tax_included_snapshot, tax_rate_snapshot, source_fingerprint
                ) VALUES (
                    'inventory_allocation', 1, 1, 2, 3, 4,
                    5, 1, 6, 1, 'pieces', 1, 1, 'CNY', 1, 0.13, ?
                )
                """,
                ("e" * 64,),
            )
        connection.rollback()

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"""
                INSERT INTO {REVERSAL_TABLE}(
                    composite_physical_group_receipt_id,
                    receipt_version_before, receipt_version_after,
                    reversed_sheet_quantity,
                    reversed_component_output_piece_quantity,
                    reversed_reserved_component_piece_quantity,
                    reason, idempotency_key, request_hash, reversed_by
                ) VALUES (
                    1, 2, 3, 3, 8, 2,
                    'duplicate', 'reversal-2', ?, 900
                )
                """,
                ("f" * 64,),
            )
        connection.rollback()
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_p1_150b_downgrade_fails_closed_when_group_facts_exist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "p1-150b-fail-closed.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, TARGET)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        _insert_group(connection)
        connection.commit()

    with pytest.raises(RuntimeError, match="P1-150B downgrade blocked"):
        command.downgrade(config, PARENT)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (TARGET,)
        assert GROUP_TABLE in _tables(connection)


def test_p1_150b_model_contract_exports_all_lineage() -> None:
    from app.models import (
        Base,
        CompositePhysicalGroupReceipt,
        CompositePhysicalGroupReceiptReversal,
        CompositePhysicalGroupReceiptSourceAllocation,
        CompositePhysicalPurchaseGroup,
        CompositePhysicalPurchaseGroupSource,
        FinanceDeliveryMaterialCostFact,
        IncomingReceiptItem,
    )

    assert CompositePhysicalPurchaseGroup.__tablename__ == GROUP_TABLE
    assert CompositePhysicalPurchaseGroupSource.__tablename__ == SOURCE_TABLE
    assert CompositePhysicalGroupReceipt.__tablename__ == RECEIPT_TABLE
    assert (
        CompositePhysicalGroupReceiptSourceAllocation.__tablename__
        == ALLOCATION_TABLE
    )
    assert CompositePhysicalGroupReceiptReversal.__tablename__ == REVERSAL_TABLE
    assert NEW_TABLES <= set(Base.metadata.tables)
    assert "component_type" not in Base.metadata.tables[GROUP_TABLE].c
    assert "component_type_snapshot" in Base.metadata.tables[SOURCE_TABLE].c
    assert (
        IncomingReceiptItem.__table__.c.composite_physical_purchase_group_id.nullable
        is True
    )
    assert (
        FinanceDeliveryMaterialCostFact.__table__.c[
            "incoming_receipt_purpose_allocation_id"
        ].nullable
        is True
    )
    assert (
        FinanceDeliveryMaterialCostFact.__table__.c[
            "composite_physical_group_receipt_id"
        ].nullable
        is True
    )
