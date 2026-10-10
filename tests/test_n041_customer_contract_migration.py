from __future__ import annotations

import hashlib
import importlib.util
from datetime import date
from decimal import Decimal
from pathlib import Path
import shutil
import sqlite3

from alembic import command
from alembic.config import Config
import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PARENT_REVISION = "ce61v8x9z50"
TARGET_REVISION = "df62v8x9z51"


def _alembic_config(monkeypatch: pytest.MonkeyPatch, database_path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(database_path.parent / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "n041-migration-test-secret")
    return Config(str(PROJECT_ROOT / "alembic.ini"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verified_backup(database_path: Path, label: str) -> Path:
    backup_path = database_path.with_name(f"{database_path.stem}-{label}.backup.sqlite3")
    shutil.copy2(database_path, backup_path)
    assert backup_path.stat().st_size == database_path.stat().st_size
    assert _sha256(backup_path) == _sha256(database_path)
    with sqlite3.connect(backup_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    return backup_path


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    return {
        row[1]
        for row in connection.execute(f"PRAGMA table_info({table_name})")
    }


def _assert_health(connection: sqlite3.Connection, revision: str) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute(
        "SELECT version_num FROM alembic_version"
    ).fetchone() == (revision,)
    assert not any(name.startswith("_alembic_tmp") for name in _tables(connection))

def test_n041_migration_is_linear_and_defines_contract_schema() -> None:
    path = (
        PROJECT_ROOT
        / "alembic"
        / "versions"
        / "df62v8x9z51_n041_customer_contract_workflow.py"
    )
    spec = importlib.util.spec_from_file_location("n041_migration", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.revision == "df62v8x9z51"
    assert module.down_revision == "ce61v8x9z50"
    source = path.read_text(encoding="utf-8")
    for table_name in (
        "contract_daily_sequences",
        "customer_contracts",
        "customer_contract_items",
    ):
        assert table_name in source
    assert "source_contract_id" in source
    assert "ck_customer_contracts_status" in source
    assert "uq_sales_orders_source_contract_id" in source


def test_contract_metadata_has_unique_conversion_boundary() -> None:
    from app.models import Base

    contracts = Base.metadata.tables["customer_contracts"]
    orders = Base.metadata.tables["sales_orders"]
    assert "converted_order_id" in contracts.c
    assert "conversion_idempotency_key" in contracts.c
    assert "source_contract_id" in orders.c
    assert any(
        set(constraint.columns.keys()) == {"source_contract_id"}
        for constraint in orders.constraints
    )


def test_n041_real_sqlite_upgrade_downgrade_upgrade_with_verified_backups(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "n041-roundtrip.sqlite3"
    config = _alembic_config(monkeypatch, database_path)

    # Build a disposable parent-revision database, then protect every N041
    # transition with a byte-identical, integrity-checked backup.
    command.upgrade(config, PARENT_REVISION)
    with sqlite3.connect(database_path) as connection:
        parent_order_columns = _columns(connection, "sales_orders")
        _assert_health(connection, PARENT_REVISION)
    _verified_backup(database_path, "before-first-upgrade")

    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(database_path) as connection:
        assert {
            "contract_daily_sequences",
            "customer_contracts",
            "customer_contract_items",
        } <= _tables(connection)
        assert _columns(connection, "sales_orders") == parent_order_columns | {
            "source_contract_id"
        }
        source_contract_fks = {
            (row[3], row[2], row[4], row[6])
            for row in connection.execute("PRAGMA foreign_key_list(sales_orders)")
            if row[3] == "source_contract_id"
        }
        assert source_contract_fks == {
            ("source_contract_id", "customer_contracts", "id", "RESTRICT")
        }
        _assert_health(connection, TARGET_REVISION)
    _verified_backup(database_path, "before-downgrade")

    command.downgrade(config, PARENT_REVISION)
    with sqlite3.connect(database_path) as connection:
        assert not {
            "contract_daily_sequences",
            "customer_contracts",
            "customer_contract_items",
        } & _tables(connection)
        assert _columns(connection, "sales_orders") == parent_order_columns
        _assert_health(connection, PARENT_REVISION)
    _verified_backup(database_path, "before-second-upgrade")

    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(database_path) as connection:
        assert {
            "contract_daily_sequences",
            "customer_contracts",
            "customer_contract_items",
        } <= _tables(connection)
        assert "source_contract_id" in _columns(connection, "sales_orders")
        _assert_health(connection, TARGET_REVISION)


def test_n041_real_sqlite_downgrade_fails_closed_with_contract_facts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from sqlalchemy.orm import Session

    from app.core.database import create_sqlite_engine
    from app.models.customer import Customer
    from app.models.customer_contract import CustomerContract, CustomerContractItem
    from app.models.product import Product
    from app.models.user import User

    database_path = tmp_path / "n041-fact-guard.sqlite3"
    config = _alembic_config(monkeypatch, database_path)
    command.upgrade(config, PARENT_REVISION)
    _verified_backup(database_path, "before-fact-upgrade")
    command.upgrade(config, TARGET_REVISION)

    engine = create_sqlite_engine(database_path)
    with Session(engine) as db:
        customer = Customer(
            customer_number=4199,
            customer_code="N041-MIGRATION-CUSTOMER",
            name="N041 迁移事实客户",
            credit_limit=0,
        )
        user = User(
            username="n041-migration-user",
            password_hash="hash",
            role="sales",
            real_name="N041 迁移用户",
            is_active=True,
            must_change_password=False,
        )
        db.add_all([customer, user])
        db.flush()
        product = Product(
            customer_id=customer.id,
            product_code="N041-MIGRATION-BOX",
            customer_material_code="N041-MIGRATION-BOX",
            product_name="N041 迁移纸箱",
            box_category="normal",
            is_active=True,
            layer_count=5,
            flute_type="AB",
        )
        db.add(product)
        db.flush()
        contract = CustomerContract(
            contract_no="CT-20260720-999",
            customer_id=customer.id,
            customer_name=customer.name,
            contract_date=date(2026, 7, 20),
            customer_po="MIGRATION-FACT",
            total_amount=Decimal("25.00"),
            status="draft",
            version=1,
            created_by=user.id,
        )
        contract.items.append(
            CustomerContractItem(
                line_no=1,
                product_id=product.id,
                product_code=product.product_code,
                product_name=product.product_name,
                quantity=10,
                unit_price=Decimal("2.5000"),
                subtotal=Decimal("25.00"),
            )
        )
        db.add(contract)
        db.commit()
        contract_id = contract.id
    engine.dispose()

    _verified_backup(database_path, "before-blocked-downgrade")
    with pytest.raises(RuntimeError, match="N041 合同事实已存在"):
        command.downgrade(config, PARENT_REVISION)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT contract_no FROM customer_contracts WHERE id = ?",
            (contract_id,),
        ).fetchone() == ("CT-20260720-999",)
        assert connection.execute(
            "SELECT COUNT(*) FROM customer_contract_items WHERE contract_id = ?",
            (contract_id,),
        ).fetchone() == (1,)
        _assert_health(connection, TARGET_REVISION)
