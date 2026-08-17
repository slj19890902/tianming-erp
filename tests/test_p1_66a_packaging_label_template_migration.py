from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory


ROOT = Path(__file__).resolve().parents[1]
PARENT = "pp24v8x9z13"
TARGET = "qq25v8x9z14"
COMPACT_TEMPLATE = "current_40x30_v2"


def _config(monkeypatch: pytest.MonkeyPatch, database: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(database.parent / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "p1-66a-label-template-test")
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database.as_posix()}")
    return config


def _table_sql(connection: sqlite3.Connection, table: str) -> str:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    assert row and row[0]
    return str(row[0])


def test_p1_66a_is_linear_and_round_trips_without_v2_facts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "p1-66a-roundtrip.sqlite3"
    config = _config(monkeypatch, database)
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    assert len(heads) == 1
    assert TARGET in {
        revision.revision
        for revision in script.iterate_revisions(heads[0], PARENT)
    }
    assert script.get_revision(TARGET).down_revision == PARENT

    command.upgrade(config, PARENT)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO customers "
            "(customer_number,customer_code,name,payment_term_days,"
            "statement_cycle_start_day,credit_limit,delivery_method,"
            "default_tax_rate,status,is_active,version) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (6601, "P166A-OLD", "迁移前客户", 0, 20, 0, "配送", 0.13, "active", 1, 1),
        )
        connection.commit()
    command.upgrade(config, TARGET)
    with sqlite3.connect(database) as connection:
        columns = {
            row[1]: row for row in connection.execute("PRAGMA table_info(customers)")
        }
        assert columns["chinese_short_name"][3] == 0
        assert columns["chinese_short_name"][4] is None
        assert connection.execute(
            "SELECT chinese_short_name FROM customers WHERE customer_code='P166A-OLD'"
        ).fetchone() == (None,)
        for table in (
            "production_tasks",
            "production_packaging_label_print_jobs",
            "production_packaging_label_print_job_tasks",
        ):
            assert COMPACT_TEMPLATE in _table_sql(connection, table)
        assert "production_packaging_label_layout_revisions" in {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    command.downgrade(config, PARENT)
    command.upgrade(config, TARGET)
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            TARGET,
        )


def test_v2_task_requires_product_version_and_blocks_downgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "p1-66a-task-fact.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, TARGET)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO production_tasks "
                "(order_item_id,production_label_template_version_snapshot) VALUES (?,?)",
                (990001, COMPACT_TEMPLATE),
            )
        connection.execute(
            "INSERT INTO production_tasks "
            "(order_item_id,production_label_template_version_snapshot,"
            "production_label_product_version_snapshot) VALUES (?,?,?)",
            (990002, COMPACT_TEMPLATE, 1),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="P1-66A"):
        command.downgrade(config, PARENT)


def test_layout_revision_fact_blocks_downgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "p1-66b-layout-fact.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, TARGET)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO production_packaging_label_layout_revisions "
            "(stream,version,catalog_version,payload_json,payload_hash,"
            "base_release_version,operation_kind) VALUES (?,?,?,?,?,?,?)",
            (
                "release",
                1,
                "p1-66b-v1",
                "{}",
                "d" * 64,
                0,
                "publish",
            ),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="P1-66B"):
        command.downgrade(config, PARENT)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            TARGET,
        )


def test_v2_print_job_fact_blocks_downgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "p1-66a-job-fact.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, TARGET)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "INSERT INTO production_packaging_label_print_jobs "
            "(supplier_order_id,idempotency_key,request_hash,template_version,"
            "plan_fingerprint,payload_json,payload_hash,status) VALUES (?,?,?,?,?,?,?,?)",
            (
                990001,
                "p1-66a-v2-job",
                "a" * 64,
                COMPACT_TEMPLATE,
                "b" * 64,
                "{}",
                "c" * 64,
                "prepared",
            ),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="P1-66A"):
        command.downgrade(config, PARENT)


def test_customer_short_name_fact_blocks_downgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "p1-66a-customer-short-name-fact.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, TARGET)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO customers "
            "(customer_number,customer_code,name,chinese_short_name,payment_term_days,"
            "statement_cycle_start_day,credit_limit,delivery_method,default_tax_rate,"
            "status,is_active,version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                6602,
                "P166A-SHORT",
                "苏州思迈尔包装有限公司",
                "思迈包装",
                0,
                20,
                0,
                "配送",
                0.13,
                "active",
                1,
                1,
            ),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="客户中文简称"):
        command.downgrade(config, PARENT)
