from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.core.database import create_sqlite_engine


ROOT = Path(__file__).resolve().parents[1]
PARENT = "iy60v8x9z49"
TARGET = "iz61v8x9z50"


def _config(monkeypatch: pytest.MonkeyPatch, path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(path))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def _health(path: Path) -> tuple[str, list[tuple]]:
    with sqlite3.connect(path) as connection:
        return (
            connection.execute("PRAGMA integrity_check").fetchone()[0],
            connection.execute("PRAGMA foreign_key_check").fetchall(),
        )


def _insert_old_cost_row(path: Path) -> tuple:
    with sqlite3.connect(path) as connection:
        center_id = connection.execute(
            "SELECT id FROM finance_cost_centers WHERE code='PROD'"
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO finance_cost_pool_entries (
                cost_month, document_date, cost_center_id,
                cost_center_code_snapshot, cost_center_name_snapshot,
                cost_center_type_snapshot, cost_category, accounting_class,
                allocation_basis, description, counterparty_name, document_number,
                amount, tax_amount, source_type, source_reference,
                source_fingerprint, note, status, version
            ) VALUES (
                '2026-08', '2026-08-31', ?, 'PROD', '生产成本', 'production',
                'production_wages', 'manufacturing', 'production_quantity',
                '八月生产工资', '员工工资', 'PAYROLL-202608',
                12345.67, 0.00, 'manual', '八月工资表',
                'P1-131-OLD-COST-ROW', '迁移前记录', 'draft', 7
            )
            """,
            (center_id,),
        )
        connection.commit()
        return connection.execute(
            """
            SELECT id, cost_month, document_date, cost_center_id,
                   cost_center_code_snapshot, cost_center_name_snapshot,
                   cost_center_type_snapshot, cost_category, accounting_class,
                   allocation_basis, description, counterparty_name,
                   document_number, amount, tax_amount, source_type,
                   source_reference, source_fingerprint, note, status, version
            FROM finance_cost_pool_entries
            WHERE source_fingerprint='P1-131-OLD-COST-ROW'
            """
        ).fetchone()


def _read_old_cost_row(path: Path) -> tuple:
    with sqlite3.connect(path) as connection:
        return connection.execute(
            """
            SELECT id, cost_month, document_date, cost_center_id,
                   cost_center_code_snapshot, cost_center_name_snapshot,
                   cost_center_type_snapshot, cost_category, accounting_class,
                   allocation_basis, description, counterparty_name,
                   document_number, amount, tax_amount, source_type,
                   source_reference, source_fingerprint, note, status, version
            FROM finance_cost_pool_entries
            WHERE source_fingerprint='P1-131-OLD-COST-ROW'
            """
        ).fetchone()


def _insert_old_snapshot(path: Path) -> tuple:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO sales_order_item_estimated_cost_snapshots (
                sales_order_item_id,order_item_reference_snapshot,snapshot_version,
                source_fingerprint,calculation_status,scope_code,rule_version,
                precision_version,order_quantity_snapshot,loss_rate,
                processing_batch_cost,processing_unit_cost,extra_color_unit_cost,
                loss_material_total_cost,processing_total_cost,one_time_fee_total,
                known_estimated_subtotal,tax_rate_reference,tax_basis_code,
                breakdown_json,missing_items_json
            ) VALUES (
                101,'P1-131-OLD-SNAPSHOT',1,?,'calculated','estimated_total',
                'p1-28c1-estimated-v1','p1-28c1-decimal-v1',100,0.03,
                0,0.250000,0,0,25.00,0,25.00,0.13,
                'material_source_as_stored','{}','[]'
            )
            """,
            ("b" * 64,),
        )
        connection.commit()
        return connection.execute(
            "SELECT id,processing_unit_cost,processing_total_cost,"
            "snapshot_version,source_fingerprint FROM "
            "sales_order_item_estimated_cost_snapshots "
            "WHERE order_item_reference_snapshot='P1-131-OLD-SNAPSHOT'"
        ).fetchone()


def _read_old_snapshot(path: Path) -> tuple:
    with sqlite3.connect(path) as connection:
        return connection.execute(
            "SELECT id,processing_unit_cost,processing_total_cost,"
            "snapshot_version,source_fingerprint FROM "
            "sales_order_item_estimated_cost_snapshots "
            "WHERE order_item_reference_snapshot='P1-131-OLD-SNAPSHOT'"
        ).fetchone()


def test_processing_cost_migration_is_linear_preserves_cost_rows_and_round_trips(
    monkeypatch,
    tmp_path,
) -> None:
    source = (
        ROOT
        / "alembic/versions/iz61v8x9z50_p1_131_processing_cost_rules.py"
    ).read_text(encoding="utf-8")
    assert 'revision = "iz61v8x9z50"' in source
    assert 'down_revision = "iy60v8x9z49"' in source
    assert [
        path.name
        for path in (ROOT / "alembic/versions").glob("*iz61v8x9z50*.py")
    ] == ["iz61v8x9z50_p1_131_processing_cost_rules.py"]

    path = tmp_path / "processing-cost-roundtrip.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, PARENT)
    before = _insert_old_cost_row(path)
    snapshot_before = _insert_old_snapshot(path)

    command.upgrade(config, TARGET)
    assert _read_old_cost_row(path) == before
    assert _read_old_snapshot(path) == snapshot_before
    engine = create_sqlite_engine(path)
    inspector = inspect(engine)
    assert {
        "processing_cost_settings",
        "product_processing_profiles",
    }.issubset(inspector.get_table_names())
    with engine.connect() as connection:
        settings = connection.exec_driver_sql(
            "SELECT working_hours_per_day,working_days_per_month,default_printer,"
            "new_printer_normal_sheets_per_minute,new_printer_max_sheets_per_minute,"
            "average_worker_monthly_salary,average_worker_monthly_social_cost,version "
            "FROM processing_cost_settings WHERE id=1"
        ).one()
        assert tuple(settings) == (8, 26, "new", 90, 120, None, 0, 1)
        index_names = {
            row[1]
            for row in connection.exec_driver_sql(
                "PRAGMA index_list('finance_cost_pool_entries')"
            ).all()
        }
        assert {
            "ix_finance_cost_pool_entries_month_status",
            "ix_finance_cost_pool_entries_center_month",
            "ix_finance_cost_pool_entries_category_month",
            "ix_finance_cost_pool_entries_source",
        }.issubset(index_names)
        foreign_keys = connection.exec_driver_sql(
            "PRAGMA foreign_key_list('finance_cost_pool_entries')"
        ).all()
        assert any(row[2] == "finance_cost_centers" for row in foreign_keys)
    engine.dispose()
    assert _health(path) == ("ok", [])
    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            connection.execute(
                "UPDATE sales_order_item_estimated_cost_snapshots "
                "SET processing_total_cost=26 WHERE "
                "order_item_reference_snapshot='P1-131-OLD-SNAPSHOT'"
            )

    # Both newly allowed wage categories pass SQLite's recreated check.  They
    # are removed again so the intentionally fail-closed downgrade remains safe.
    with sqlite3.connect(path) as connection:
        for category, accounting_class, center_code in (
            ("driver_wages", "selling", "WH_DELIVERY"),
            ("finance_wages", "finance", "FINANCE"),
        ):
            center = connection.execute(
                "SELECT id,name,center_type FROM finance_cost_centers WHERE code=?",
                (center_code,),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO finance_cost_pool_entries (
                    cost_month,document_date,cost_center_id,
                    cost_center_code_snapshot,cost_center_name_snapshot,
                    cost_center_type_snapshot,cost_category,accounting_class,
                    allocation_basis,description,amount,tax_amount,source_type,
                    source_reference,status,version
                ) VALUES ('2026-09','2026-09-01',?,?,?,?,?,?,
                          'unallocated',?,1.00,0.00,'manual',?,'draft',1)
                """,
                (
                    center[0],
                    center_code,
                    center[1],
                    center[2],
                    category,
                    accounting_class,
                    f"{category}测试",
                    f"{category}迁移测试",
                ),
            )
        assert connection.execute(
            "SELECT COUNT(*) FROM finance_cost_pool_entries "
            "WHERE cost_category IN ('driver_wages','finance_wages')"
        ).fetchone()[0] == 2
        connection.execute(
            "DELETE FROM finance_cost_pool_entries "
            "WHERE cost_category IN ('driver_wages','finance_wages')"
        )
        connection.commit()

    command.downgrade(config, PARENT)
    assert _read_old_cost_row(path) == before
    assert _read_old_snapshot(path) == snapshot_before
    assert _health(path) == ("ok", [])
    command.upgrade(config, TARGET)
    assert _read_old_cost_row(path) == before
    assert _read_old_snapshot(path) == snapshot_before
    assert _health(path) == ("ok", [])


def test_processing_cost_downgrade_fails_closed_after_business_changes(
    monkeypatch,
    tmp_path,
) -> None:
    from app.models.customer import Customer
    from app.models.processing_cost import ProductProcessingProfile
    from app.models.product import Product

    path = tmp_path / "processing-cost-fail-closed.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, TARGET)
    engine = create_sqlite_engine(path)
    with Session(engine) as db:
        customer = Customer(customer_code="MIG-PROC", name="迁移加工测试客户")
        db.add(customer)
        db.flush()
        product = Product(
            customer_id=customer.id,
            product_code="MIG-PROC-001",
            customer_material_code="MIG-PROC-001",
            product_name="迁移加工测试产品",
            box_category="normal",
        )
        db.add(product)
        db.flush()
        db.add(
            ProductProcessingProfile(
                product_id=product.id,
                printer_mode="old",
                die_cut_mode="none",
            )
        )
        db.commit()
    engine.dispose()

    with pytest.raises(RuntimeError, match="product processing profiles"):
        command.downgrade(config, PARENT)

    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM product_processing_profiles")
        connection.execute(
            "UPDATE processing_cost_settings "
            "SET average_worker_monthly_salary=5000, version=2"
        )
        connection.commit()
    with pytest.raises(RuntimeError, match="processing settings differ"):
        command.downgrade(config, PARENT)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE processing_cost_settings "
            "SET average_worker_monthly_salary=NULL, version=1"
        )
        connection.execute(
            """
            INSERT INTO sales_order_item_estimated_cost_snapshots (
                sales_order_item_id,order_item_reference_snapshot,snapshot_version,
                source_fingerprint,calculation_status,scope_code,rule_version,
                precision_version,order_quantity_snapshot,loss_rate,
                processing_batch_cost,processing_unit_cost,extra_color_unit_cost,
                loss_material_total_cost,processing_total_cost,one_time_fee_total,
                known_estimated_subtotal,tax_rate_reference,tax_basis_code,
                breakdown_json,missing_items_json
            ) VALUES (
                202,'P1-131-NULL-PROCESSING',1,?,'partial','estimated_total',
                'p1-131-estimated-v2','p1-28c1-decimal-v1',100,0.03,
                0,NULL,0,0,NULL,0,0,0.13,
                'material_source_as_stored','{}','[""salary""]'
            )
            """,
            ("c" * 64,),
        )
        connection.commit()
    with pytest.raises(RuntimeError, match="incomplete processing-cost snapshots"):
        command.downgrade(config, PARENT)
    assert _health(path) == ("ok", [])
