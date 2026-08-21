from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest


ROOT = Path(__file__).resolve().parents[1]
PARENT = "vv30v8x9z19"
TARGET = "yy33v8x9z22"
PROTECTED = (ROOT / "data" / "carton_erp.sqlite3").resolve()


def _config(monkeypatch: pytest.MonkeyPatch, path: Path) -> Config:
    resolved = path.resolve()
    assert resolved != PROTECTED
    monkeypatch.setenv("ERP_DATABASE_PATH", str(resolved))
    monkeypatch.setenv("ERP_SECRET_KEY", "p1-82-isolated-migration-only")
    monkeypatch.setenv("ERP_BACKUP_DIR", str(resolved.parent / "backups"))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{resolved.as_posix()}")
    return config


def _health(connection: sqlite3.Connection, revision: str) -> None:
    connection.execute("PRAGMA foreign_keys=ON")
    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
        revision,
    )


def _seed_parent(connection: sqlite3.Connection) -> tuple[int, int, int, int]:
    user_id = connection.execute(
        "INSERT INTO users(username,password_hash,role,real_name,display_name,"
        "must_change_password) VALUES('p182-admin','x','admin','P182','P182',0) "
        "RETURNING id"
    ).fetchone()[0]
    customer_ids = []
    for index, (code, name, short_name) in enumerate(
        (
            ("P182-A", "P1-82客户甲", "甲"),
            ("P182-B", "P1-82客户乙", "乙"),
        ),
        start=1,
    ):
        customer_ids.append(
            connection.execute(
                "INSERT INTO customers(customer_number,customer_code,name,"
                "chinese_short_name) VALUES(?,?,?,?) RETURNING id",
                (9820 + index, code, name, short_name),
            ).fetchone()[0]
        )
    mold_id = connection.execute(
        "INSERT INTO mold_tools(mold_code,mold_name,rack_location,created_by,updated_by) "
        "VALUES('P182-LEGACY','旧模具','1F-M-R01-L1-G01',?,?) RETURNING id",
        (user_id, user_id),
    ).fetchone()[0]
    connection.execute(
        "INSERT INTO products(customer_id,product_code,customer_material_code,"
        "product_name,mold_tool_id) VALUES(?,?,?,?,?)",
        (customer_ids[0], "P182-P", "P182-M", "P1-82旧产品", mold_id),
    )
    connection.commit()
    return user_id, customer_ids[0], customer_ids[1], mold_id


def test_mold_identity_migration_roundtrip_backfill_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "p1-82-migration.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, PARENT)
    with sqlite3.connect(path) as connection:
        user_id, customer_a, customer_b, mold_id = _seed_parent(connection)
        _health(connection, PARENT)

    command.upgrade(config, TARGET)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT label_name,chinese_short_name,identity_status,version "
            "FROM mold_tools WHERE id=?",
            (mold_id,),
        ).fetchone() == (None, None, "legacy_unset", 1)
        assert connection.execute(
            "SELECT customer_id,display_order FROM mold_tool_customers "
            "WHERE mold_tool_id=?",
            (mold_id,),
        ).fetchall() == [(customer_a, 1)]
        triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND tbl_name='mold_master_mutations'"
            )
        }
        assert triggers == {
            "trg_mold_master_mutations_no_update",
            "trg_mold_master_mutations_no_delete",
        }
        _health(connection, TARGET)

    command.downgrade(config, PARENT)
    command.upgrade(config, TARGET)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO mold_tool_customers(mold_tool_id,customer_id,display_order,created_by) "
            "VALUES(?,?,2,?)",
            (mold_id, customer_b, user_id),
        )
        connection.execute(
            "UPDATE mold_tools SET label_name='92.5*36.5*1.8/2',"
            "chinese_short_name='中性内盒',identity_status='frozen',version=2 "
            "WHERE id=?",
            (mold_id,),
        )
        connection.commit()
        _health(connection, TARGET)

    with pytest.raises(RuntimeError, match="cannot downgrade"):
        command.downgrade(config, PARENT)
    with sqlite3.connect(path) as connection:
        _health(connection, TARGET)
