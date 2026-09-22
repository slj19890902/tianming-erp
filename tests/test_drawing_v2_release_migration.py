"""V2 schema roundtrip from the release baseline in a disposable empty DB.

This does not use a production export or establish factory-data compatibility.
"""
from argparse import Namespace
import json
from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory


ROOT = Path(__file__).resolve().parents[1]
BASE = "dw0922"
TARGET = "dx0922"
TABLES = {
    "drawing_designs", "drawing_number_sequences", "drawing_releases",
    "production_task_drawings", "production_task_drawing_adoptions",
}
TRIGGERS = {
    "drawing_releases_no_update": ("drawing_releases", "UPDATE", "manifest_json"),
    "drawing_releases_no_delete": ("drawing_releases", "DELETE", None),
    "production_task_drawings_no_update": ("production_task_drawings", "UPDATE", "release_id"),
    "production_task_drawings_no_delete": ("production_task_drawings", "DELETE", None),
    "task_drawing_adoptions_no_update": ("production_task_drawing_adoptions", "UPDATE", "request_hash"),
    "task_drawing_adoptions_no_delete": ("production_task_drawing_adoptions", "DELETE", None),
}


def _config(database, root, monkeypatch):
    root = root.resolve()
    database = database.resolve()
    assert database.parent == root and not database.exists()
    assert database != (ROOT / "data" / "carton_erp.sqlite3").resolve()
    for key, value in {
        "ERP_DATABASE_PATH": database,
        "ERP_FILE_STORAGE_DIR": root / "files",
        "ERP_BACKUP_DIR": root / "backups",
        "ERP_TEMP_DIR": root / "temp",
        "ERP_SECRET_KEY_FILE": root / "secret.key",
        "ERP_SECRET_KEY": "drawing-v2-disposable-schema-test-only",
        "ERP_ENVIRONMENT": "test",
        "ERP_ALLOWED_ORIGINS": "http://127.0.0.1:18999",
        "ERP_UAT_ROOT": "",
    }.items():
        monkeypatch.setenv(key, str(value))
    from app.core.config import load_settings
    assert load_settings().database_path == database
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.cmd_opts = Namespace(x=[f"expected_database_path={database}"])
    return config


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _schema(connection):
    return {
        (kind, name): (table, sql)
        for kind, name, table, sql in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        )
    }


def _rows(connection, table_names):
    return {
        table: sorted(connection.execute(f"SELECT * FROM {_quote(table)}").fetchall(), key=repr)
        for table in table_names if table != "alembic_version"
    }


def _revision_and_integrity(connection, expected):
    assert connection.execute("SELECT version_num FROM alembic_version").fetchall() == [(expected,)]
    assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _assert_added_schema(connection, original_schema):
    current = _schema(connection)
    assert {name for kind, name in current if kind == "table"} - {
        name for kind, name in original_schema if kind == "table"
    } == TABLES
    assert {name for kind, name in current if kind == "trigger"} - {
        name for kind, name in original_schema if kind == "trigger"
    } == set(TRIGGERS)
    for name, (table, operation, field) in TRIGGERS.items():
        sql = current[("trigger", name)][1].upper()
        assert f"BEFORE {operation} ON {table.upper()}" in sql
        assert "RAISE(ABORT," in sql and "IMMUTABLE" in sql
        # Compile the actual trigger programs without inserting release/task facts.
        statement = (f"UPDATE {_quote(table)} SET {_quote(field)}={_quote(field)}"
                     if field else f"DELETE FROM {_quote(table)}")
        program = connection.execute("EXPLAIN " + statement).fetchall()
        assert any(row[1] == "Halt" and row[2] == sqlite3.SQLITE_CONSTRAINT_TRIGGER for row in program)
        # Newer SQLite stores the RAISE message in a preceding String8 register;
        # older engines may keep it in Halt's P4. Both compile the same guard.
        assert any("immutable" in str(row[5]) for row in program)
    for table, required in {
        "drawing_releases": {
            ("product_id", "external_number", "revision"),
            ("product_id", "design_version", "product_version"),
            ("product_id", "idempotency_key"),
        },
        "production_task_drawing_adoptions": {
            ("idempotency_key",), ("task_id", "resulting_task_version"),
        },
    }.items():
        unique = {
            tuple(row[2] for row in connection.execute(f"PRAGMA index_info({_quote(index[1])})"))
            for index in connection.execute(f"PRAGMA index_list({_quote(table)})") if index[2]
        }
        assert required <= unique


def test_release_drawing_schema_roundtrip_preserves_baseline(tmp_path, monkeypatch):
    database = tmp_path / "drawing-release-roundtrip.sqlite3"
    config = _config(database, tmp_path, monkeypatch)
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == [TARGET]
    assert scripts.get_revision(TARGET).down_revision == BASE

    command.upgrade(config, BASE)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO customers(customer_number,customer_code,name) VALUES (922001,?,?)",
            ("DW-DX-ANONYMOUS", "Drawing migration synthetic customer"),
        )
        connection.commit()
        _revision_and_integrity(connection, BASE)
        original_schema = _schema(connection)
        original_tables = {name for kind, name in original_schema if kind == "table"}
        original_rows = _rows(connection, original_tables)

    for operation, revision in ((command.upgrade, TARGET), (command.downgrade, BASE), (command.upgrade, TARGET)):
        operation(config, revision)
        with sqlite3.connect(database) as connection:
            _revision_and_integrity(connection, revision)
            current = _schema(connection)
            assert all(current.get(key) == value for key, value in original_schema.items())
            assert _rows(connection, original_tables) == original_rows
            if revision == TARGET:
                _assert_added_schema(connection, original_schema)
            else:
                assert current == original_schema

    print(json.dumps({
        "database": str(database), "source": "empty database plus one synthetic customer",
        "roundtrip": [BASE, TARGET, BASE, TARGET],
        "original_table_count": len(original_tables),
        "original_explicit_index_count": sum(kind == "index" for kind, _ in original_schema),
        "original_trigger_count": sum(kind == "trigger" for kind, _ in original_schema),
        "added_tables": sorted(TABLES), "immutable_triggers": sorted(TRIGGERS),
        "original_rows_schema_indexes_triggers": "unchanged at every step",
        "integrity_check": "ok", "foreign_key_check_count": 0,
        "factory_database_rehearsal": False,
    }, ensure_ascii=False))
