from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config


def _config(database_path: Path) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{database_path.as_posix()}")
    return config


def test_fin001_migration_roundtrip_and_fail_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "fin001-migration.sqlite3"
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    config = _config(database_path)
    command.upgrade(config, "dc85v8x9z74")
    connection = sqlite3.connect(database_path)
    assert connection.execute("pragma quick_check").fetchone()[0] == "ok"
    assert not connection.execute("pragma foreign_key_check").fetchall()
    assert connection.execute("select version_num from alembic_version").fetchone()[0] == "dc85v8x9z74"
    assert connection.execute("select 1 from sqlite_master where type='table' and name='finance_invoice_tasks'").fetchone()
    connection.close()
    command.downgrade(config, "db84v8x9z73")
    command.upgrade(config, "dc85v8x9z74")
    connection = sqlite3.connect(database_path)
    connection.execute("insert into invoice_seller_entities (seller_code,seller_name) values ('S','匿名')")
    connection.commit()
    connection.close()
    try:
        command.downgrade(config, "db84v8x9z73")
    except RuntimeError as error:
        assert "禁止破坏性降级" in str(error)
    else:  # pragma: no cover - migration must never silently delete FIN-001 facts
        raise AssertionError("FIN-001 facts should block downgrade")
