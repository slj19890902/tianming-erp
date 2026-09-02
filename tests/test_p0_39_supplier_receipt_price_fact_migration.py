from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory


ROOT = Path(__file__).resolve().parents[1]
PARENT = "jm71v8x9z60"
TARGET = "jn72v8x9z61"
FACT_TABLE = "supplier_receipt_settlement_price_facts"
ADOPTION_REASON = "2026-09-02 老板确认采用当前主数据"


def _load_migration_module():
    path = ROOT / "alembic" / "versions" / f"{TARGET}_supplier_receipt_settlement_price_facts.py"
    spec = importlib.util.spec_from_file_location("p0_39_price_fact_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config(monkeypatch: pytest.MonkeyPatch, database: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(database.parent / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "p0-39-receipt-price-fact-migration")
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return config


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _health(connection: sqlite3.Connection, revision: str) -> None:
    connection.execute("PRAGMA foreign_keys=ON")
    assert connection.execute(
        "SELECT version_num FROM alembic_version"
    ).fetchone() == (revision,)
    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _insert_receipt_item(
    connection: sqlite3.Connection,
    *,
    receipt_id: int,
    item_id: int,
    stock_item_id: int,
) -> None:
    connection.execute(
        """
        INSERT INTO incoming_receipts(
            id, receipt_number, status, received_at, idempotency_key
        ) VALUES (?, ?, 'posted', '2026-09-02 02:30:00', ?)
        """,
        (receipt_id, f"IR-P039-{receipt_id}", f"ir-p039-{receipt_id}"),
    )
    connection.execute(
        """
        INSERT INTO incoming_receipt_items(
            id, receipt_id, stock_replenishment_item_id,
            planned_quantity, received_quantity,
            cumulative_received_quantity, variance_quantity,
            variance_type, resolution_status, status
        ) VALUES (
            ?, ?, ?, 20, 20, 20, 0,
            'matched', 'not_required', 'posted'
        )
        """,
        (item_id, receipt_id, stock_item_id),
    )


def _insert_price_fact(
    connection: sqlite3.Connection,
    *,
    receipt_item_id: int,
    fact_origin: str,
    adoption_reason: str | None,
    source_hash: str,
    adoption_evidence_reference: str | None = None,
    shipping_fee_mode: str = "included",
) -> int:
    return int(
        connection.execute(
            f"""
            INSERT INTO {FACT_TABLE}(
                incoming_receipt_item_id, supplier_id,
                supplier_name_snapshot, material_id,
                material_code_snapshot, source_material_version,
                source_kind, purchase_document_number_snapshot,
                receipt_number_snapshot, receipt_date_snapshot,
                received_quantity_snapshot,
                quantity_unit, report_length_mm, report_width_mm,
                unit_price, price_unit, currency, tax_included, tax_rate,
                shipping_fee_mode,
                fact_origin, match_strategy, source_hash,
                adoption_reason, adoption_evidence_reference, created_by
            ) VALUES (
                ?, 1, '测试纸板供应商', 1,
                'K=A', 3, 'stock_replenishment_item', 'PO-P039',
                ?, '2026-09-02', 20, '张', 1200, 800,
                2.500000, 'per_sheet', 'CNY', 1, 0.13, ?,
                ?, 'stable_material_id', ?, ?, ?, 1
            ) RETURNING id
            """,
            (
                receipt_item_id,
                f"IR-P039-{receipt_item_id}",
                shipping_fee_mode,
                fact_origin,
                source_hash,
                adoption_reason,
                adoption_evidence_reference,
            ),
        ).fetchone()[0]
    )


def test_p0_39_price_fact_migration_is_linear_round_trips_and_never_backfills(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "p0-39-round-trip.sqlite3"
    config = _config(monkeypatch, database)
    script = ScriptDirectory.from_config(config)
    assert len(script.get_heads()) == 1
    assert TARGET in {
        revision.revision for revision in script.walk_revisions("base", script.get_heads()[0])
    }
    assert script.get_revision(TARGET).down_revision == PARENT

    command.upgrade(config, PARENT)
    with sqlite3.connect(database) as connection:
        # Foreign-key enforcement stays off only for this synthetic parent row.
        # It proves the migration does not silently adopt any pre-existing receipt.
        _insert_receipt_item(
            connection, receipt_id=1, item_id=1, stock_item_id=999
        )
        connection.execute(
            """
            INSERT INTO supplier_monthly_statements(
                id, statement_number, supplier_id, supplier_name_snapshot,
                settlement_month, period_start, period_end, currency,
                tax_basis, status, erp_amount, adjustment_amount, adjusted_amount
            ) VALUES (
                1, 'AP-P039-PARENT', 1, '历史供应商', '2026-08',
                '2026-07-21', '2026-08-20', 'CNY', 'tax_inclusive',
                'draft', 50, 0, 50
            )
            """
        )
        connection.execute(
            """
            INSERT INTO supplier_monthly_statement_lines(
                id, statement_id, source_type, source_key,
                incoming_receipt_item_id, purchase_document_number,
                receipt_number, receipt_date, category_label,
                material_or_product_snapshot, received_quantity,
                quantity_unit, frozen_unit_price, price_unit, currency,
                tax_basis, tax_rate, erp_amount, tax_amount, source_link
            ) VALUES (
                1, 1, 'paperboard', 'paperboard:1', 1, 'PO-P039-PARENT',
                'IR-P039-1', '2026-08-20', '瓦楞纸板', 'K=A',
                20, '张', 2.5, 'per_sheet', 'CNY', 'tax_inclusive',
                0.13, 50, 5.75, '/incoming.html?receipt_item_id=1'
            )
            """
        )
        connection.commit()

    command.upgrade(config, TARGET)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            f"SELECT COUNT(*) FROM {FACT_TABLE}"
        ).fetchone() == (0,)
        assert "supplier_receipt_price_fact_id" in _columns(
            connection, "supplier_monthly_statement_lines"
        )
        assert "procurement_route_snapshot" in _columns(
            connection, "stock_replenishment_order_items"
        )
        assert connection.execute(
            "SELECT source_key, supplier_receipt_price_fact_id "
            "FROM supplier_monthly_statement_lines WHERE id=1"
        ).fetchone() == ("paperboard:1", None)
        fact_columns = _columns(connection, FACT_TABLE)
        assert {
            "incoming_receipt_item_id",
            "supplier_id",
            "material_id",
            "source_kind",
            "purchase_document_number_snapshot",
            "receipt_number_snapshot",
            "receipt_date_snapshot",
            "received_quantity_snapshot",
            "report_length_mm",
            "report_width_mm",
            "unit_price",
            "shipping_fee_mode",
            "fact_origin",
            "match_strategy",
            "source_hash",
            "adoption_reason",
            "adoption_evidence_reference",
            "created_by",
            "created_at",
        } <= fact_columns
        trigger_names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
        assert {
            "trg_supplier_receipt_price_facts_immutable_update",
            "trg_supplier_receipt_price_facts_immutable_delete",
            "trg_supplier_statement_receipt_reverse_guard",
            "trg_supplier_statement_price_fact_match_insert",
            "trg_supplier_statement_price_fact_match_update",
            "trg_stock_replenishment_route_validate_insert",
            "trg_stock_replenishment_route_immutable_update",
        } <= trigger_names
        line_fks = list(
            connection.execute(
                "PRAGMA foreign_key_list(supplier_monthly_statement_lines)"
            )
        )
        assert any(
            row[2] == FACT_TABLE
            and row[3] == "supplier_receipt_price_fact_id"
            and row[6] == "RESTRICT"
            for row in line_fks
        )
        connection.execute(
            """
            INSERT INTO stock_replenishment_orders(
                id, order_number, source_type, status
            ) VALUES (999, 'SR-P039-ROUTE', 'customer_request', 'confirmed')
            """
        )
        connection.execute(
            """
            INSERT INTO stock_replenishment_order_items(
                id, replenishment_order_id, target_inventory_type,
                procurement_route_snapshot, product_name_snapshot,
                sheet_type, component_type, pieces_per_box,
                stock_yield_per_sheet, quantity, stocked_quantity
            ) VALUES (
                999, 999, 'semi_finished', 'paperboard', '路线冻结测试',
                'raw_board', 'whole', 1, 1, 1, 0
            )
            """
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError, match="invalid stock"):
            connection.execute(
                """
                INSERT INTO stock_replenishment_order_items(
                    id, replenishment_order_id, target_inventory_type,
                    procurement_route_snapshot, product_name_snapshot,
                    sheet_type, component_type, pieces_per_box,
                    stock_yield_per_sheet, quantity, stocked_quantity
                ) VALUES (
                    1000, 999, 'semi_finished', 'wrong', '非法路线测试',
                    'raw_board', 'whole', 1, 1, 1, 0
                )
                """
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE stock_replenishment_order_items "
                "SET procurement_route_snapshot='external_packaging' WHERE id=999"
            )
        connection.rollback()
        connection.execute(
            "DELETE FROM stock_replenishment_order_items WHERE id=999"
        )
        connection.execute("DELETE FROM stock_replenishment_orders WHERE id=999")
        connection.execute("DELETE FROM supplier_monthly_statement_lines WHERE id=1")
        connection.execute("DELETE FROM supplier_monthly_statements WHERE id=1")
        connection.execute("DELETE FROM incoming_receipt_items WHERE id=1")
        connection.execute("DELETE FROM incoming_receipts WHERE id=1")
        connection.commit()
        _health(connection, TARGET)

    command.downgrade(config, PARENT)
    with sqlite3.connect(database) as connection:
        _health(connection, PARENT)
        assert FACT_TABLE not in {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "supplier_receipt_price_fact_id" not in _columns(
            connection, "supplier_monthly_statement_lines"
        )
        assert "procurement_route_snapshot" not in _columns(
            connection, "stock_replenishment_order_items"
        )

    command.upgrade(config, TARGET)
    with sqlite3.connect(database) as connection:
        _health(connection, TARGET)


def test_postgresql_line_link_uses_direct_ddl_without_table_rebuild() -> None:
    migration = _load_migration_module()

    class PostgresOperations:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        def batch_alter_table(self, *_args, **_kwargs):
            raise AssertionError("PostgreSQL must not rebuild the referenced line table")

        def add_column(self, *_args, **_kwargs):
            self.calls.append("add_column")

        def create_foreign_key(self, *_args, **_kwargs):
            self.calls.append("create_foreign_key")

        def create_check_constraint(self, *_args, **_kwargs):
            self.calls.append("create_check_constraint")

        def create_index(self, *_args, **_kwargs):
            self.calls.append("create_index")

        def drop_index(self, *_args, **_kwargs):
            self.calls.append("drop_index")

        def drop_constraint(self, *_args, **_kwargs):
            self.calls.append("drop_constraint")

        def drop_column(self, *_args, **_kwargs):
            self.calls.append("drop_column")

    operations = PostgresOperations()
    migration.op = operations
    migration._add_statement_line_price_fact_link()
    migration._drop_statement_line_price_fact_link()
    assert operations.calls == [
        "add_column",
        "create_foreign_key",
        "create_check_constraint",
        "create_index",
        "drop_index",
        "drop_constraint",
        "drop_constraint",
        "drop_column",
    ]


def test_postgresql_stock_route_snapshot_uses_direct_ddl_without_table_rebuild() -> None:
    migration = _load_migration_module()

    class PostgresOperations:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        def batch_alter_table(self, *_args, **_kwargs):
            raise AssertionError("PostgreSQL must not rebuild stock replenishment items")

        def add_column(self, *_args, **_kwargs):
            self.calls.append("add_column")

        def create_check_constraint(self, *_args, **_kwargs):
            self.calls.append("create_check_constraint")

        def execute(self, *_args, **_kwargs):
            self.calls.append("execute")

        def drop_constraint(self, *_args, **_kwargs):
            self.calls.append("drop_constraint")

        def drop_column(self, *_args, **_kwargs):
            self.calls.append("drop_column")

    operations = PostgresOperations()
    migration.op = operations
    migration._add_stock_route_snapshot()
    migration._drop_stock_route_snapshot()
    assert operations.calls == [
        "add_column",
        "create_check_constraint",
        "execute",
        "execute",
        "execute",
        "execute",
        "drop_constraint",
        "drop_column",
    ]


def test_p0_39_price_facts_enforce_origin_immutability_link_and_downgrade_guard(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "p0-39-facts.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, TARGET)

    with sqlite3.connect(database) as connection:
        _insert_receipt_item(connection, receipt_id=1, item_id=1, stock_item_id=999)
        _insert_receipt_item(connection, receipt_id=2, item_id=2, stock_item_id=1000)

        with pytest.raises(sqlite3.IntegrityError):
            _insert_price_fact(
                connection,
                receipt_item_id=1,
                fact_origin="historical_master_adoption",
                adoption_reason="当前主数据",
                source_hash="a" * 64,
            )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_price_fact(
                connection,
                receipt_item_id=1,
                fact_origin="historical_master_adoption",
                adoption_reason=ADOPTION_REASON,
                source_hash="e" * 64,
                adoption_evidence_reference=None,
            )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_price_fact(
                connection,
                receipt_item_id=1,
                fact_origin="receipt_frozen",
                adoption_reason=ADOPTION_REASON,
                source_hash="b" * 64,
            )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_price_fact(
                connection,
                receipt_item_id=1,
                fact_origin="receipt_frozen",
                adoption_reason=None,
                source_hash="f" * 64,
                shipping_fee_mode="not_provided",
            )

        historical_fact_id = _insert_price_fact(
            connection,
            receipt_item_id=1,
            fact_origin="historical_master_adoption",
            adoption_reason=ADOPTION_REASON,
            source_hash="c" * 64,
            adoption_evidence_reference="backup-p0-39-20260902.sqlite3",
        )
        current_fact_id = _insert_price_fact(
            connection,
            receipt_item_id=2,
            fact_origin="receipt_frozen",
            adoption_reason=None,
            source_hash="d" * 64,
        )
        connection.commit()

        assert connection.execute(
            f"SELECT fact_origin, adoption_reason, adoption_evidence_reference, "
            f"shipping_fee_mode "
            f"FROM {FACT_TABLE} ORDER BY id"
        ).fetchall() == [
            (
                "historical_master_adoption",
                ADOPTION_REASON,
                "backup-p0-39-20260902.sqlite3",
                "included",
            ),
            ("receipt_frozen", None, None, "included"),
        ]
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                f"UPDATE {FACT_TABLE} SET unit_price=3 WHERE id=?",
                (historical_fact_id,),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                f"DELETE FROM {FACT_TABLE} WHERE id=?", (current_fact_id,)
            )
        connection.rollback()

        connection.execute(
            """
            INSERT INTO supplier_monthly_statements(
                id, statement_number, supplier_id, supplier_name_snapshot,
                settlement_month, period_start, period_end, currency,
                tax_basis, status, erp_amount, adjustment_amount, adjusted_amount
            ) VALUES (
                1, 'AP-P039', 1, '测试纸板供应商', '2026-09',
                '2026-08-21', '2026-09-20', 'CNY', 'tax_inclusive',
                'draft', 50, 0, 50
            )
            """
        )
        connection.execute(
            """
            INSERT INTO supplier_monthly_statement_lines(
                statement_id, source_type, source_key,
                incoming_receipt_item_id, supplier_receipt_price_fact_id,
                purchase_document_number, receipt_number, receipt_date,
                category_label, material_or_product_snapshot,
                received_quantity, quantity_unit, frozen_unit_price,
                price_unit, currency, tax_basis, tax_rate,
                erp_amount, tax_amount, source_link
            ) VALUES (
                1, 'paperboard', 'paperboard:1', 1, ?,
                'PO-P039', 'IR-P039-1', '2026-09-02', '瓦楞纸板', 'K=A',
                20, '张', 2.5, 'per_sheet', 'CNY', 'tax_inclusive',
                0.13, 50, 5.75, '/incoming.html?receipt_item_id=1'
            )
            """,
            (historical_fact_id,),
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError, match="does not match"):
            connection.execute(
                """
                INSERT INTO supplier_monthly_statement_lines(
                    statement_id, source_type, source_key,
                    incoming_receipt_item_id, supplier_receipt_price_fact_id,
                    purchase_document_number, receipt_number, receipt_date,
                    category_label, material_or_product_snapshot,
                    received_quantity, quantity_unit, frozen_unit_price,
                    price_unit, currency, tax_basis, tax_rate,
                    erp_amount, tax_amount, source_link
                ) VALUES (
                    1, 'paperboard', 'paperboard:2', 2, ?,
                    'PO-P039-2', 'IR-P039-2', '2026-09-02', '瓦楞纸板', 'K=A',
                    20, '张', 2.5, 'per_sheet', 'CNY', 'tax_inclusive',
                    0.13, 50, 5.75, '/incoming.html?receipt_item_id=2'
                )
                """,
                (historical_fact_id,),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="does not match"):
            connection.execute(
                """
                INSERT INTO supplier_monthly_statement_lines(
                    statement_id, source_type, source_key,
                    incoming_receipt_item_id, supplier_receipt_price_fact_id,
                    purchase_document_number, receipt_number, receipt_date,
                    category_label, material_or_product_snapshot,
                    received_quantity, quantity_unit, frozen_unit_price,
                    price_unit, currency, tax_basis, tax_rate,
                    erp_amount, tax_amount, source_link
                ) VALUES (
                    1, 'paperboard', 'paperboard:2', 2, ?,
                    'PO-P039', 'IR-P039-2', '2026-09-03', '瓦楞纸板', 'K=A',
                    20, '张', 2.5, 'per_sheet', 'CNY', 'tax_inclusive',
                    0.13, 50, 5.75, '/incoming.html?receipt_item_id=2'
                )
                """,
                (current_fact_id,),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="does not match"):
            connection.execute(
                """
                UPDATE supplier_monthly_statement_lines
                SET received_quantity = 19
                WHERE incoming_receipt_item_id = 1
                """
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO supplier_monthly_statement_lines(
                    statement_id, source_type, source_key,
                    external_receipt_item_id, supplier_receipt_price_fact_id,
                    purchase_document_number, receipt_number, receipt_date,
                    category_label, material_or_product_snapshot,
                    received_quantity, quantity_unit, frozen_unit_price,
                    price_unit, currency, tax_basis, tax_rate,
                    erp_amount, tax_amount, source_link
                ) VALUES (
                    1, 'external_packaging', 'external:999', 999, ?,
                    'EP-P039', 'ER-P039', '2026-09-02', '外购包材', '护角',
                    1, '件', 10, 'per_piece', 'CNY', 'tax_inclusive',
                    0.13, 10, 1.15, '/external-receipts/999'
                )
                """,
                (historical_fact_id,),
            )
        connection.rollback()

    with pytest.raises(RuntimeError, match="downgrade blocked"):
        command.downgrade(config, PARENT)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (TARGET,)
        assert connection.execute(
            f"SELECT COUNT(*) FROM {FACT_TABLE}"
        ).fetchone() == (2,)
