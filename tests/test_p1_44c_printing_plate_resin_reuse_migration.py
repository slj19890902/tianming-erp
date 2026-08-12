from __future__ import annotations

from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
import pytest


ROOT = Path(__file__).resolve().parents[1]
PARENT = "gg15v8x9z04"
TARGET = "hh16v8x9z05"


def _config(monkeypatch: pytest.MonkeyPatch, path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(path))
    monkeypatch.setenv("ERP_SECRET_KEY", "p1-44c-migration-test")
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def _insert_parent_facts(connection: sqlite3.Connection) -> tuple[int, int, int, int]:
    user_id = connection.execute(
        "INSERT INTO users (username,password_hash,role,real_name,is_active,auth_version,"
        "must_change_password,customer_access_mode,ui_mode) "
        "VALUES ('p144c-admin','hash','admin','P144C',1,1,0,'all','standard') RETURNING id"
    ).fetchone()[0]
    customer_a = connection.execute(
        "INSERT INTO customers (customer_number,customer_code,name,payment_term_days,"
        "credit_limit,is_active) VALUES (44031,'P144C-A','迁移旧客户',30,100000,1) "
        "RETURNING id"
    ).fetchone()[0]
    customer_b = connection.execute(
        "INSERT INTO customers (customer_number,customer_code,name,payment_term_days,"
        "credit_limit,is_active) VALUES (44032,'P144C-B','迁移新客户',30,100000,1) "
        "RETURNING id"
    ).fetchone()[0]
    plate_id = connection.execute(
        "INSERT INTO printing_plates (plate_code,customer_id,plate_name,color_name,"
        "rack_location,status,version,location_version,created_by,updated_by) "
        "VALUES ('PL440301',?,'迁移旧版','红色','1F-PL-R01-L2-P01','active',1,1,?,?) "
        "RETURNING id",
        (customer_a, user_id, user_id),
    ).fetchone()[0]
    connection.commit()
    return user_id, customer_a, customer_b, plate_id


def _assert_health(connection: sqlite3.Connection, revision: str) -> None:
    connection.execute("PRAGMA foreign_keys=ON")
    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
        revision,
    )


def test_resin_reuse_migration_round_trip_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "p1-44c-migration.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, PARENT)
    with sqlite3.connect(path) as connection:
        user_id, customer_a, customer_b, plate_id = _insert_parent_facts(connection)
        _assert_health(connection, PARENT)

    command.upgrade(config, TARGET)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='printing_plate_resin_reuses'"
        ).fetchone() == ("printing_plate_resin_reuses",)
        _assert_health(connection, TARGET)

    command.downgrade(config, PARENT)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='printing_plate_resin_reuses'"
        ).fetchone() is None
        _assert_health(connection, PARENT)

    command.upgrade(config, TARGET)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO printing_plate_resin_reuses (printing_plate_id,"
            "plate_code_snapshot,from_customer_id,from_customer_name_snapshot,"
            "from_plate_name_snapshot,from_color_name_snapshot,to_customer_id,"
            "to_customer_name_snapshot,to_plate_name_snapshot,to_color_name_snapshot,"
            "rack_location_snapshot,actor_id,actor_username_snapshot,idempotency_key,"
            "expected_version,resulting_version,old_resin_removed,new_resin_mounted) "
            "VALUES (?,'PL440301',?,'迁移旧客户','迁移旧版','红色',?,'迁移新客户',"
            "'迁移新版','蓝色','1F-PL-R01-L2-P01',?,'p144c-admin','p144c-migration-0001',"
            "1,2,1,1)",
            (plate_id, customer_a, customer_b, user_id),
        )
        connection.commit()
        _assert_health(connection, TARGET)
    with pytest.raises(RuntimeError, match="拒绝破坏性降级"):
        command.downgrade(config, PARENT)
    with sqlite3.connect(path) as connection:
        _assert_health(connection, TARGET)
