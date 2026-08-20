from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


ROOT = Path(__file__).resolve().parents[1]
VERSIONS = ROOT / "alembic" / "versions"
BASE_REVISION = "vv30v8x9z19"


def _purpose_migration_path() -> Path:
    matches = sorted(VERSIONS.glob("*purchase_purpose*.py"))
    assert len(matches) == 1, [path.name for path in matches]
    return matches[0]


def _load_migration():
    path = _purpose_migration_path()
    spec = importlib.util.spec_from_file_location("p1_80_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    # alembic/env.py intentionally replaces sqlalchemy.url with application
    # settings.  Both gates are required so tests can never fall back to the
    # checkout database when the config object is evaluated in a new process.
    resolved_db = db_path.resolve()
    protected_checkout_db = (ROOT / "data" / "carton_erp.sqlite3").resolve()
    assert resolved_db != protected_checkout_db
    monkeypatch.setenv("ERP_DATABASE_PATH", str(resolved_db))
    monkeypatch.setenv("ERP_ENVIRONMENT", "development")
    monkeypatch.setenv(
        "ERP_SECRET_KEY",
        "p1-80-anonymous-migration-test-secret-key",
    )
    monkeypatch.setenv("ERP_BACKUP_DIR", str((db_path.parent / "backups").resolve()))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return config


def _pragma(db_path: Path, statement: str):
    with sqlite3.connect(db_path) as connection:
        return connection.execute(statement).fetchall()


def test_migration_is_linear_and_schema_expresses_frozen_purpose_facts() -> None:
    module = _load_migration()
    assert module.down_revision == BASE_REVISION
    assert module.branch_labels is None

    source = _purpose_migration_path().read_text(encoding="utf-8")
    for expected in (
        "purchase_sheet_qty",
        "order_purpose_sheet_qty",
        "reserve_purpose_sheet_qty",
        "snapshot_version",
        "preview_fingerprint",
        "request_hash",
        "request_actor_id",
        "customer_id",
        "order_item_id",
        "supplier_requisition_order_item_id",
    ):
        assert expected in source
    assert "UPDATE SUPPLIER_REQUISITION" not in source.upper()
    assert "BEFORE UPDATE" in source
    assert "BEFORE DELETE" in source


def test_empty_sqlite_roundtrip_upgrade_downgrade_upgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "p1_80_empty.sqlite3"
    config = _config(db_path, monkeypatch)
    command.upgrade(config, BASE_REVISION)
    before_tables = set(inspect(create_engine(config.get_main_option("sqlalchemy.url"))).get_table_names())

    module = _load_migration()
    command.upgrade(config, module.revision)
    engine = create_engine(config.get_main_option("sqlalchemy.url"))
    upgraded_tables = set(inspect(engine).get_table_names())
    purpose_tables = {name for name in upgraded_tables if "purpose" in name}
    assert purpose_tables
    assert upgraded_tables >= before_tables
    assert _pragma(db_path, "PRAGMA integrity_check") == [("ok",)]
    assert _pragma(db_path, "PRAGMA foreign_key_check") == []

    command.downgrade(config, BASE_REVISION)
    downgraded_tables = set(inspect(engine).get_table_names())
    assert not ({name for name in downgraded_tables if "purpose" in name})
    assert _pragma(db_path, "PRAGMA integrity_check") == [("ok",)]
    assert _pragma(db_path, "PRAGMA foreign_key_check") == []

    command.upgrade(config, module.revision)
    assert {name for name in inspect(engine).get_table_names() if "purpose" in name}
    assert _pragma(db_path, "PRAGMA integrity_check") == [("ok",)]
    assert _pragma(db_path, "PRAGMA foreign_key_check") == []


def test_upgrade_keeps_legacy_orders_unset_without_guessing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "p1_80_legacy.sqlite3"
    config = _config(db_path, monkeypatch)
    command.upgrade(config, BASE_REVISION)
    engine = create_engine(config.get_main_option("sqlalchemy.url"))
    with engine.begin() as connection:
        user_id = connection.execute(
            text(
                "INSERT INTO users (username,password_hash,role,real_name,"
                "must_change_password,is_active) VALUES "
                "('legacy-p180','x','admin','legacy',0,1) RETURNING id"
            )
        ).scalar_one()
        order_id = connection.execute(
            text(
                "INSERT INTO supplier_requisition_orders "
                "(order_number,supplier_name,total_quantity,stock_deduction_qty,"
                "requisition_qty,status,created_by) VALUES "
                "('SRO-LEGACY-P180','匿名供应商',600,0,600,'confirmed',:uid) "
                "RETURNING id"
            ),
            {"uid": user_id},
        ).scalar_one()
        connection.execute(
            text(
                "INSERT INTO supplier_requisition_order_items "
                "(supplier_order_id,quantity,stock_deduction_qty,requisition_qty,"
                "status,version) VALUES (:oid,500,0,600,'active',1)"
            ),
            {"oid": order_id},
        )

    before = engine.connect().execute(
        text(
            "SELECT total_quantity,stock_deduction_qty,requisition_qty,status "
            "FROM supplier_requisition_orders WHERE id=:id"
        ),
        {"id": order_id},
    ).one()
    module = _load_migration()
    command.upgrade(config, module.revision)
    after = engine.connect().execute(
        text(
            "SELECT total_quantity,stock_deduction_qty,requisition_qty,status "
            "FROM supplier_requisition_orders WHERE id=:id"
        ),
        {"id": order_id},
    ).one()
    assert tuple(after) == tuple(before)

    purpose_tables = [
        name for name in inspect(engine).get_table_names() if "purpose" in name
    ]
    assert purpose_tables
    for table in purpose_tables:
        assert engine.connect().execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar_one() == 0


def test_downgrade_fails_closed_when_any_new_purpose_fact_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "p1_80_fact.sqlite3"
    config = _config(db_path, monkeypatch)
    module = _load_migration()
    command.upgrade(config, module.revision)
    engine = create_engine(config.get_main_option("sqlalchemy.url"))
    purpose_tables = [
        name for name in inspect(engine).get_table_names() if "purpose" in name
    ]
    assert purpose_tables
    table = purpose_tables[0]
    columns = {column["name"] for column in inspect(engine).get_columns(table)}
    assert "purchase_sheet_qty" in columns

    # A downgrade guard must reject even a single complete anonymous fact.
    # The fixture is inserted through the mapped model so the migration never
    # needs a test-only helper and every production constraint remains active.
    from sqlalchemy.orm import Session

    from app.core.security import hash_password
    from app.models.customer import Customer
    from app.models.supplier_requisition_order import (
        PurchasePurposeSourceSnapshot,
        SupplierRequisitionOrder,
        SupplierRequisitionOrderItem,
    )
    from app.models.user import User

    with Session(engine) as session:
        user = User(
            username="p180-migration",
            password_hash=hash_password("AnonymousPass123!"),
            role="admin",
            real_name="匿名迁移用户",
            must_change_password=False,
        )
        customer = Customer(
            customer_number=180,
            customer_code="P180-ANON",
            name="匿名客户",
        )
        session.add_all([user, customer])
        session.flush()
        supplier_order = SupplierRequisitionOrder(
            order_number="SRO-P180-ANON",
            supplier_name="匿名供应商",
            total_quantity=1,
            stock_deduction_qty=0,
            requisition_qty=1,
            status="confirmed",
            created_by=user.id,
        )
        session.add(supplier_order)
        session.flush()
        supplier_item = SupplierRequisitionOrderItem(
            supplier_order_id=supplier_order.id,
            order_item_id=None,
            source_key="direct_supplier_item:anonymous",
            product_id=None,
            product_code="P180-ANON-P",
            product_name="匿名纸箱",
            quantity=1,
            stock_deduction_qty=0,
            requisition_qty=1,
            cutting_mode="一开一",
            pieces_per_box=1,
            required_piece_qty=1,
            customer_name=customer.name,
        )
        session.add(supplier_item)
        session.flush()
        session.add(
            PurchasePurposeSourceSnapshot(
                snapshot_key="p180-anonymous-fact",
                allocation_group_key="p180-anonymous-group",
                supplier_requisition_order_item_id=supplier_item.id,
                material_requisition_item_id=None,
                source_kind="direct_supplier_item",
                source_key="direct_supplier_item:anonymous",
                source_order_item_id=None,
                source_requisition_item_id=None,
                source_bom_requisition_source_id=None,
                customer_id=customer.id,
                customer_name_snapshot=customer.name,
                component_type="whole",
                source_finished_qty_snapshot=1,
                pieces_per_finished_snapshot=1,
                source_required_piece_qty_snapshot=1,
                source_semi_reserved_piece_qty_snapshot=0,
                source_effective_piece_qty_snapshot=1,
                yield_per_sheet_snapshot=1,
                group_effective_piece_qty_snapshot=1,
                group_authoritative_order_sheet_qty_snapshot=1,
                purchase_sheet_qty=1,
                order_purpose_sheet_qty=1,
                reserve_purpose_sheet_qty=0,
                calculation_rule_version="p1-80-v1",
                snapshot_version=1,
                preview_fingerprint="a" * 64,
                request_hash="b" * 64,
                created_by=user.id,
            )
        )
        session.commit()

    with pytest.raises(Exception):
        command.downgrade(config, BASE_REVISION)
    with engine.connect() as connection:
        revision = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
        assert revision == module.revision
        assert connection.execute(
            text(f'SELECT COUNT(*) FROM "{table}"')
        ).scalar_one() >= 1
    assert _pragma(db_path, "PRAGMA integrity_check") == [("ok",)]
    assert _pragma(db_path, "PRAGMA foreign_key_check") == []
