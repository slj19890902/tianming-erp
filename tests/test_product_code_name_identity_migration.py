from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import create_sqlite_engine
from app.models.customer import Customer
from app.models.product import Product


ROOT = Path(__file__).resolve().parents[1]
PARENT_REVISION = "dz08v8x9z97"
TARGET_REVISION = "ea09v8x9z98"


def _create_parent_schema(database: Path) -> None:
    from sqlalchemy import Column, Integer, MetaData, String, Table, UniqueConstraint

    engine = create_sqlite_engine(database)
    metadata = MetaData()
    Table("alembic_version", metadata, Column("version_num", String, primary_key=True))
    Table(
        "customers", metadata, Column("id", Integer, primary_key=True),
        Column("customer_number", Integer, nullable=False), Column("customer_code", String, nullable=False),
        Column("name", String, nullable=False),
    )
    Table(
        "products", metadata, Column("id", Integer, primary_key=True),
        Column("customer_id", Integer, nullable=False), Column("product_code", String, nullable=False),
        Column("customer_material_code", String, nullable=False), Column("product_name", String, nullable=False),
        Column("box_category", String, nullable=False), Column("is_internal_component", Integer, nullable=False, server_default="0"),
        UniqueConstraint("customer_id", "product_code", name="uq_products_customer_product_code"),
        UniqueConstraint("customer_id", "customer_material_code", name="uq_products_customer_material_code"),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("INSERT INTO alembic_version(version_num) VALUES ('dz08v8x9z97')")
    engine.dispose()


def _config(monkeypatch: pytest.MonkeyPatch, database: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(database.parent / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "product-code-name-migration-test-secret")
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database.as_posix()}")
    return config


def _seed_one_product(database: Path) -> None:
    engine = create_sqlite_engine(database)
    with Session(engine) as session:
        customer_id = session.execute(
            text(
                """INSERT INTO customers(customer_number,customer_code,name)
                VALUES (1,'YL','迁移测试客户') RETURNING id"""
            )
        ).scalar_one()
        session.execute(
            text(
                """INSERT INTO products(
                customer_id,product_code,customer_material_code,product_name,box_category
                ) VALUES (:customer_id,'Z.001.000093','Z.001.000093',
                '双路ECU新版纸盒','normal')"""
            ),
            {"customer_id": customer_id},
        )
        session.commit()
    engine.dispose()


def _insert_same_code_child(database: Path) -> None:
    engine = create_sqlite_engine(database)
    with Session(engine) as session:
        session.execute(
            text(
                """INSERT INTO products(
                customer_id,product_code,customer_material_code,product_name,
                box_category,is_internal_component
                ) VALUES (1,'Z.001.000093','Z.001.000093',
                '双路ECU新版纸盒内衬','normal',1)"""
            )
        )
        session.commit()
    engine.dispose()


def test_product_code_name_identity_round_trip(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "product-code-name-round-trip.sqlite3"
    _create_parent_schema(database)
    config = _config(monkeypatch, database)
    _seed_one_product(database)

    command.upgrade(config, TARGET_REVISION)
    _insert_same_code_child(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT product_name FROM products WHERE product_code=? ORDER BY id",
            ("Z.001.000093",),
        ).fetchall() == [
            ("双路ECU新版纸盒",),
            ("双路ECU新版纸盒内衬",),
        ]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO products "
                "(customer_id,product_code,customer_material_code,product_name,box_category) "
                "VALUES (1,?,?,?,?)",
                (
                    "Z.001.000093",
                    "Z.001.000093",
                    "双路ECU新版纸盒内衬",
                    "normal",
                ),
            )
        connection.execute("DELETE FROM products WHERE product_name LIKE '%内衬'")
        connection.commit()

    command.downgrade(config, PARENT_REVISION)
    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (TARGET_REVISION,)


def test_product_code_name_downgrade_fails_closed_with_same_code_facts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "product-code-name-downgrade-guard.sqlite3"
    _create_parent_schema(database)
    config = _config(monkeypatch, database)
    command.upgrade(config, TARGET_REVISION)
    _seed_one_product(database)
    _insert_same_code_child(database)

    with pytest.raises(RuntimeError, match="已有同客户同码、不同名称的产品"):
        command.downgrade(config, PARENT_REVISION)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (TARGET_REVISION,)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_order_picker_frontend_requests_authoritative_selection_context() -> None:
    index = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert index.count('selection_context:"order"') == 2
    assert index.count('.filter(Boolean).join("｜") || "未命名常用箱"') == 2
