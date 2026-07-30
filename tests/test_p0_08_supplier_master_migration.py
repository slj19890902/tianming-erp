from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import sqlite3

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect

from app.core.database import create_sqlite_engine


ROOT = Path(__file__).resolve().parents[1]
PARENT_REVISION = "cv78v8x9z67"
TARGET_REVISION = "cw79v8x9z68"


def _config(monkeypatch: pytest.MonkeyPatch, path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(path))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def _checks(path: Path) -> tuple[str, int]:
    with sqlite3.connect(path) as connection:
        return (
            connection.execute("PRAGMA integrity_check").fetchone()[0],
            len(connection.execute("PRAGMA foreign_key_check").fetchall()),
        )


def _rows_digest(connection: sqlite3.Connection, table: str) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid'):
        digest.update(repr(row).encode("utf-8"))
        digest.update(b"\n")
        count += 1
    return count, digest.hexdigest()


def _legacy_supplier_snapshot(path: Path) -> dict:
    with sqlite3.connect(path) as connection:
        supplier_schema = connection.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE type = 'table' AND name = 'suppliers'
            """
        ).fetchone()
        assert supplier_schema is not None, "正式来源结构必须保留旧 suppliers 表"
        referenced: dict[str, dict] = {}
        table_names = [
            row[0]
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            )
        ]
        for table in table_names:
            foreign_keys = connection.execute(
                f'PRAGMA foreign_key_list("{table}")'
            ).fetchall()
            if not any(row[2] == "suppliers" for row in foreign_keys):
                continue
            referenced[table] = {
                "schema": connection.execute(
                    """
                    SELECT sql FROM sqlite_master
                    WHERE type = 'table' AND name = ?
                    """,
                    (table,),
                ).fetchone()[0],
                "foreign_keys": foreign_keys,
                "rows": _rows_digest(connection, table),
            }
        assert referenced, "正式来源结构必须至少有一张表引用旧 suppliers"
        return {
            "schema": supplier_schema[0],
            "columns": connection.execute(
                "PRAGMA table_info(suppliers)"
            ).fetchall(),
            "indexes": connection.execute(
                "PRAGMA index_list(suppliers)"
            ).fetchall(),
            "rows": _rows_digest(connection, "suppliers"),
            "referenced": referenced,
        }


def test_supplier_migration_is_linear_and_model_matches() -> None:
    source = (
        ROOT / "alembic/versions/cw79v8x9z68_supplier_master.py"
    ).read_text(encoding="utf-8")
    assert 'revision: str = "cw79v8x9z68"' in source
    assert (
        'down_revision: Union[str, Sequence[str], None] = "cv78v8x9z67"'
        in source
    )
    assert "供应商主档已产生业务事实或审计记录" in source

    from app.models import Base

    supplier = Base.metadata.tables["supplier_master_records"]
    aliases = Base.metadata.tables["supplier_master_aliases"]
    assert {
        "standard_name",
        "normalized_name",
        "display_name",
        "business_code",
        "contact_name",
        "phone",
        "remarks",
        "sort_order",
        "is_active",
        "version",
    } <= set(supplier.c.keys())
    assert {"supplier_id", "alias_name", "normalized_alias"} <= set(
        aliases.c.keys()
    )


def test_supplier_migration_round_trip_and_seed_states(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "supplier-roundtrip.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, TARGET_REVISION)
    inspector = inspect(create_sqlite_engine(path))
    assert {
        "supplier_master_records",
        "supplier_master_aliases",
    } <= set(inspector.get_table_names())
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            """
            SELECT standard_name, display_name, is_active, sort_order
            FROM supplier_master_records
            ORDER BY sort_order
            """
        ).fetchall()
        assert rows == [
            ("苏州嘉林亿", "嘉林亿", 1, 10),
            ("昆山鸣朋", "鸣朋", 1, 20),
            ("胜源", "胜源", 1, 30),
            ("森林阳光", "森林阳光", 1, 40),
            ("苏州佳丰", "佳丰", 0, 90),
        ]
    assert _checks(path) == ("ok", 0)

    command.downgrade(config, PARENT_REVISION)
    inspector = inspect(create_sqlite_engine(path))
    assert "supplier_master_records" not in set(inspector.get_table_names())
    assert "supplier_master_aliases" not in set(inspector.get_table_names())
    assert _checks(path) == ("ok", 0)

    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == TARGET_REVISION
    assert _checks(path) == ("ok", 0)


def test_supplier_migration_preserves_real_legacy_supplier_table_and_fks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = Path(
        os.environ.get(
            "ERP_SUPPLIER_MIGRATION_SOURCE",
            (
                r"D:\tm-uat\q0-04-supplier-master-20260730"
                r"\carton_erp_before_q0_04.sqlite3"
            ),
        )
    )
    if not source.exists():
        pytest.skip("未提供包含旧 suppliers 表的隔离工厂来源副本")

    source_hash_before = hashlib.sha256(source.read_bytes()).hexdigest()
    path = tmp_path / "supplier-real-schema-roundtrip.sqlite3"
    shutil.copy2(source, path)
    legacy_before = _legacy_supplier_snapshot(path)
    config = _config(monkeypatch, path)

    command.upgrade(config, TARGET_REVISION)
    assert _legacy_supplier_snapshot(path) == legacy_before
    assert {
        "supplier_master_records",
        "supplier_master_aliases",
    } <= set(inspect(create_sqlite_engine(path)).get_table_names())
    assert _checks(path) == ("ok", 0)

    command.downgrade(config, PARENT_REVISION)
    assert _legacy_supplier_snapshot(path) == legacy_before
    assert "supplier_master_records" not in set(
        inspect(create_sqlite_engine(path)).get_table_names()
    )
    assert _checks(path) == ("ok", 0)

    command.upgrade(config, TARGET_REVISION)
    assert _legacy_supplier_snapshot(path) == legacy_before
    assert _checks(path) == ("ok", 0)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash_before


@pytest.mark.parametrize("fact_kind", ("supplier", "audit"))
def test_supplier_migration_downgrade_fails_closed_after_use(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fact_kind: str,
) -> None:
    path = tmp_path / f"supplier-fail-closed-{fact_kind}.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        if fact_kind == "supplier":
            connection.execute(
                """
                INSERT INTO supplier_master_records(
                    standard_name, normalized_name, display_name,
                    is_active, sort_order, version
                )
                VALUES ('新供应商', '新供应商', '新供应商', 1, 100, 1)
                """
            )
        else:
            connection.execute(
                """
                INSERT INTO operation_logs(
                    action, resource, entity_type, entity_id
                )
                VALUES ('CREATE', 'Supplier', 'supplier', 4)
                """
            )
        connection.commit()

    with pytest.raises(RuntimeError, match="禁止破坏性降级"):
        command.downgrade(config, PARENT_REVISION)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == TARGET_REVISION
    assert _checks(path) == ("ok", 0)
