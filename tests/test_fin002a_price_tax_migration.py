from __future__ import annotations

import sqlite3
import os
import subprocess
import sys
from pathlib import Path

import pytest


PARENT = "fk46v8x9z35"
TARGET = "fl47v8x9z36"


def _run_alembic(
    database_path: Path,
    *arguments: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["ERP_DATABASE_PATH"] = str(database_path.resolve())
    environment["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(root / "alembic.ini"),
            "-x",
            f"expected_database_path={database_path.resolve()}",
            *arguments,
        ],
        cwd=root,
        env=environment,
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _schema_fingerprint(database_path: Path) -> tuple[object, ...]:
    with sqlite3.connect(database_path) as connection:
        version = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchall()
        schema = connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='table' AND name IN "
            "('sales_order_items','finance_statement_items',"
            "'customer_invoice_item_rules') ORDER BY name"
        ).fetchall()
    return (version, schema)


def _online_backup(source: Path, target: Path) -> None:
    with sqlite3.connect(source, timeout=30) as source_connection:
        with sqlite3.connect(target) as target_connection:
            source_connection.backup(target_connection)


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def test_fin002a_snapshot_migration_roundtrip_and_fail_closed(
    tmp_path: Path,
) -> None:
    database = tmp_path / "fin002a.sqlite3"
    source = os.environ.get("FIN002A_ISOLATED_BASELINE")
    if not source:
        pytest.skip(
            "FIN002A_ISOLATED_BASELINE is required; the migration test only uses a copied formal baseline."
        )
    source_path = Path(source)
    assert source_path.is_file()
    source_fingerprint = _schema_fingerprint(source_path)
    assert source_fingerprint[0] == [(PARENT,)]
    _online_backup(source_path, database)

    try:
        _run_alembic(database, "upgrade", TARGET)
        with sqlite3.connect(database) as connection:
            assert connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()[0] == TARGET
            for table in ("sales_order_items", "finance_statement_items"):
                assert {"price_tax_mode_snapshot", "tax_rate_snapshot"} <= _columns(
                    connection, table
                )
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert not connection.execute("PRAGMA foreign_key_check").fetchall()

        _run_alembic(database, "downgrade", PARENT)
        _run_alembic(database, "upgrade", TARGET)

        with sqlite3.connect(database) as connection:
            order_item_id = connection.execute(
                "SELECT id FROM sales_order_items ORDER BY id LIMIT 1"
            ).fetchone()[0]
            connection.execute(
                "UPDATE sales_order_items SET price_tax_mode_snapshot='tax_inclusive', "
                "tax_rate_snapshot=0.13 WHERE id=?",
                (order_item_id,),
            )
            connection.commit()

        failed_downgrade = _run_alembic(
            database,
            "downgrade",
            PARENT,
            check=False,
        )
        assert failed_downgrade.returncode != 0
        assert "cannot downgrade FIN-002A" in (
            failed_downgrade.stdout + failed_downgrade.stderr
        )

        with sqlite3.connect(database) as connection:
            assert connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()[0] == TARGET
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        assert _schema_fingerprint(source_path) == source_fingerprint
