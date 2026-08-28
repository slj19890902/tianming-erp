from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config


def _config(database_path: Path) -> Config:
    config = Config("alembic.ini")
    config.set_main_option(
        "sqlalchemy.url", f"sqlite+pysqlite:///{database_path.as_posix()}"
    )
    return config


def test_customer_charge_migration_roundtrip_and_downgrade_guard(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "customer-charge-migration.sqlite3"
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    config = _config(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE finance_statement_items (
                id INTEGER PRIMARY KEY,
                statement_id INTEGER NOT NULL,
                return_receipt_item_id INTEGER NOT NULL UNIQUE,
                actual_received_quantity INTEGER NOT NULL,
                unit_price_snapshot NUMERIC NOT NULL,
                unit_cost_snapshot NUMERIC NOT NULL,
                receivable_amount NUMERIC NOT NULL,
                gross_profit_amount NUMERIC NOT NULL,
                price_tax_mode_snapshot TEXT,
                tax_rate_snapshot NUMERIC
            );
            CREATE TABLE finance_invoice_task_items (
                id INTEGER PRIMARY KEY,
                task_id INTEGER NOT NULL,
                sequence_no INTEGER NOT NULL,
                statement_item_id INTEGER NOT NULL,
                product_code_snapshot TEXT,
                product_name_snapshot TEXT,
                project_name TEXT NOT NULL,
                tax_classification_code TEXT NOT NULL,
                specification TEXT,
                unit TEXT NOT NULL,
                quantity NUMERIC NOT NULL,
                unit_price NUMERIC,
                amount NUMERIC NOT NULL,
                tax_rate NUMERIC NOT NULL,
                tax_amount NUMERIC NOT NULL,
                rule_version INTEGER NOT NULL
            );
            CREATE TABLE customers (id INTEGER PRIMARY KEY);
            CREATE TABLE sales_orders (id INTEGER PRIMARY KEY);
            CREATE TABLE sales_order_items (id INTEGER PRIMARY KEY);
            CREATE TABLE mold_tools (id INTEGER PRIMARY KEY);
            CREATE TABLE printing_plates (id INTEGER PRIMARY KEY);
            CREATE TABLE users (id INTEGER PRIMARY KEY);
            """
        )
    command.stamp(config, "gp51v8x9z40")
    command.upgrade(config, "gq52v8x9z41")
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("pragma quick_check").fetchone()[0] == "ok"
        assert not connection.execute("pragma foreign_key_check").fetchall()
        assert connection.execute("select version_num from alembic_version").fetchone()[0] == "gq52v8x9z41"
        statement_columns = {
            row[1] for row in connection.execute("pragma table_info(finance_statement_items)")
        }
        assert {"customer_charge_id", "charge_quantity_snapshot", "unit_snapshot"}.issubset(statement_columns)
    command.downgrade(config, "gp51v8x9z40")
    command.upgrade(config, "gq52v8x9z41")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO finance_customer_charges (
                customer_id, order_id, charge_type, display_name, quantity,
                unit, unit_price, amount, status, version
            ) VALUES (999, 999, 'other', 'downgrade guard', 1, '项', 1, 1, 'draft', 1)
            """
        )
        connection.commit()
    try:
        command.downgrade(config, "gp51v8x9z40")
    except RuntimeError as error:
        assert "cannot downgrade P1-90" in str(error)
    else:  # pragma: no cover
        raise AssertionError("customer charge facts must block destructive downgrade")
