from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker


def test_phase3_migration_script_can_run_directly() -> None:
    project_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            str(project_root / "scripts" / "migrate_phase3_data.py"),
            "--help",
        ],
        cwd=project_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "--dry-run" in result.stdout


def test_product_unique_constraints_are_scoped_to_customer(tmp_path: Path) -> None:
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.customer import Customer
    from app.models.product import Product

    engine = create_sqlite_engine(tmp_path / "models.sqlite3")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as session:
        customer = Customer(
            customer_number=1,
            customer_code="TH",
            name="苏州天华超净科技股份有限公司",
            payment_term_days=30,
            credit_limit=0,
        )
        session.add(customer)
        session.flush()
        session.add(
            Product(
                customer_id=customer.id,
                product_code="TH001",
                customer_material_code="MAT001",
                product_name="五层加强纸箱",
                box_category="normal",
            )
        )
        session.commit()

        session.add(
            Product(
                customer_id=customer.id,
                product_code="TH001",
                customer_material_code="MAT002",
                product_name="重复产品",
                box_category="normal",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_phase3_schema_migration_preserves_product_archives(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE customers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                customer_code TEXT,
                credit_terms TEXT
            );
            CREATE TABLE product_archives (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL,
                style_no TEXT NOT NULL,
                product_name TEXT,
                length_mm REAL,
                width_mm REAL,
                height_mm REAL,
                material TEXT,
                process_note TEXT,
                last_sale_unit_price REAL,
                sale_unit_price_no_tax REAL,
                last_cost_unit_price REAL
            );
            INSERT INTO customers (id, name) VALUES (1, '测试客户');
            INSERT INTO product_archives (
                id, customer_id, style_no, product_name, length_mm, width_mm,
                height_mm, material, process_note, last_sale_unit_price
            ) VALUES (
                10, 1, 'P-001', '测试纸箱', 380, 260, 220, 'K=A-BC',
                'die_cut_required=False', 2.5
            );
            """
        )
        connection.commit()

    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "phase3-migration-test")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        customer_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(customers)")
        }
        archive_count = connection.execute(
            "SELECT COUNT(*) FROM product_archives"
        ).fetchone()[0]
        archive_row = connection.execute(
            "SELECT id, style_no, product_name FROM product_archives"
        ).fetchone()

    assert {"materials", "products", "migration_entity_map"} <= tables
    assert {
        "customer_number",
        "customer_code",
        "payment_term_days",
        "credit_limit",
    } <= customer_columns
    assert archive_count == 1
    assert archive_row == (10, "P-001", "测试纸箱")

    migration_source = next(
        (Path(__file__).resolve().parents[1] / "alembic" / "versions").glob(
            "*phase3*"
        )
    ).read_text(encoding="utf-8")
    assert "drop_table('product_archives')" not in migration_source
    assert 'drop_table("product_archives")' not in migration_source


def test_phase3_data_migration_supports_dry_run_and_is_idempotent(
    tmp_path: Path,
) -> None:
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.customer import Customer
    from app.models.material import Material
    from app.models.migration import MigrationEntityMap
    from app.models.product import Product
    from scripts.migrate_phase3_data import migrate_phase3_data

    target_path = tmp_path / "target.sqlite3"
    source_path = tmp_path / "boxerp.sqlite3"
    engine = create_sqlite_engine(target_path)
    Base.metadata.create_all(engine)
    with sqlite3.connect(target_path) as connection:
        connection.execute(
            """
            CREATE TABLE product_archives (
                id INTEGER PRIMARY KEY,
                customer_id INTEGER NOT NULL,
                style_no TEXT NOT NULL,
                customer_po TEXT,
                product_name TEXT,
                length_mm REAL,
                width_mm REAL,
                height_mm REAL,
                material TEXT,
                flute_type TEXT,
                layer_count INTEGER,
                process_note TEXT,
                last_sale_unit_price REAL,
                sale_unit_price_no_tax REAL,
                last_cost_unit_price REAL,
                drawing_path TEXT,
                die_cut_path TEXT,
                remark TEXT,
                print_color TEXT,
                craft_requirements TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO product_archives VALUES (
                100, 1, 'TH001', 'PO-1', '纸箱1', 600, 400, 300,
                'B416B-AB/EB', 'AB', 5,
                'box_type=普通摇盖; die_cut_required=False; color=蓝色',
                3.6, 3.1, 2.7, NULL, NULL, '历史备注', '蓝色', '钉箱'
            )
            """
        )
        connection.commit()

    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as session:
        session.add(
            Customer(
                id=1,
                customer_number=None,
                customer_code=None,
                name="苏州天华超净科技股份有限公司",
                payment_term_days=0,
                credit_limit=0,
            )
        )
        session.commit()

    with sqlite3.connect(source_path) as connection:
        connection.executescript(
            """
            CREATE TABLE customers (
                customer_code TEXT,
                customer_name TEXT,
                default_payment_term INTEGER,
                credit_limit REAL,
                customer_number INTEGER
            );
            CREATE TABLE materials (
                material_id INTEGER PRIMARY KEY,
                code TEXT,
                layer_count INTEGER,
                flute_type TEXT,
                basis_weight_description TEXT,
                paper_composition TEXT,
                quote_price REAL,
                price_unit TEXT,
                supplier_name TEXT,
                quote_date TEXT,
                remarks TEXT
            );
            INSERT INTO customers VALUES (
                'TH', '苏州天华超净科技股份有限公司', 30, 100000, 1
            );
            INSERT INTO materials VALUES (
                1, 'B416B-AB/EB', 5, 'AB/EB', '五层标准材质',
                'B416B', 1.95, '元/平方米', '嘉林亿', '2026-04-10', NULL
            );
            """
        )
        connection.commit()

    report_path = tmp_path / "migration_report_phase3.log"
    dry_run = migrate_phase3_data(
        target_database=target_path,
        boxerp_database=source_path,
        report_path=report_path,
        dry_run=True,
    )
    assert dry_run.materials_created == 1
    assert dry_run.products_created == 1

    with session_factory() as session:
        assert session.scalar(select(Material).limit(1)) is None
        assert session.scalar(select(Product).limit(1)) is None
        assert session.scalar(select(MigrationEntityMap).limit(1)) is None
        customer = session.get(Customer, 1)
        assert customer.customer_code is None

    first = migrate_phase3_data(
        target_database=target_path,
        boxerp_database=source_path,
        report_path=report_path,
        dry_run=False,
    )
    second = migrate_phase3_data(
        target_database=target_path,
        boxerp_database=source_path,
        report_path=report_path,
        dry_run=False,
    )

    assert first.products_created == 1
    assert second.products_created == 0
    assert second.products_skipped == 1
    with session_factory() as session:
        assert len(session.scalars(select(Material)).all()) == 1
        products = session.scalars(select(Product)).all()
        mappings = session.scalars(select(MigrationEntityMap)).all()
        customer = session.get(Customer, 1)

    assert len(products) == 1
    assert products[0].box_category == "normal"
    assert products[0].material_id is not None
    assert len(mappings) == 1
    assert mappings[0].source_id == "100"
    assert customer.customer_number == 1
    assert customer.customer_code == "TH"
    assert customer.payment_term_days == 30
    assert float(customer.credit_limit) == 100000
    assert report_path.exists()


def test_die_cut_detection_prefers_explicit_process_flag() -> None:
    from scripts.migrate_phase3_data import detect_box_category

    assert detect_box_category(
        process_note="die_cut_required=True",
        die_cut_path=None,
        product_name="普通外箱",
        style_no="P-001",
    ) == "die_cut"
    assert detect_box_category(
        process_note="die_cut_required=False",
        die_cut_path=None,
        product_name="普通外箱",
        style_no="P-002",
    ) == "normal"
