from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect, text


ROOT = Path(__file__).resolve().parents[1]
PARENT = "ec0927xl"
TARGET = "ed0928ml"


def _config(monkeypatch: pytest.MonkeyPatch, database: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return config


def _mold_schema_signature(engine) -> tuple[list[tuple], list[tuple], list[tuple]]:
    with engine.connect() as connection:
        columns = [
            (
                column["name"],
                str(column["type"]),
                bool(column["nullable"]),
                str(column["default"]),
                int(column["primary_key"]),
            )
            for column in inspect(connection).get_columns("mold_tools")
        ]
        indexes = connection.execute(
            text(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type = 'index' AND tbl_name = 'mold_tools' ORDER BY name"
            )
        ).all()
        triggers = connection.execute(
            text(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type = 'trigger' AND tbl_name = 'mold_tools' ORDER BY name"
            )
        ).all()
    return columns, indexes, triggers


def test_ed0928_native_column_roundtrip_preserves_mold_schema_and_blocks_loss(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "mold-label-content.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, PARENT)
    engine = create_engine(f"sqlite:///{database}")
    before_columns, before_indexes, before_triggers = _mold_schema_signature(engine)
    engine.dispose()

    command.upgrade(config, TARGET)
    engine = create_engine(f"sqlite:///{database}")
    upgraded_columns, upgraded_indexes, upgraded_triggers = _mold_schema_signature(engine)
    assert [column for column in upgraded_columns if column[0] != "label_overrides_json"] == before_columns
    assert next(column for column in upgraded_columns if column[0] == "label_overrides_json")[1:3] == ("TEXT", True)
    assert upgraded_indexes == before_indexes
    assert upgraded_triggers == before_triggers
    engine.dispose()

    command.downgrade(config, PARENT)
    engine = create_engine(f"sqlite:///{database}")
    assert _mold_schema_signature(engine) == (
        before_columns,
        before_indexes,
        before_triggers,
    )
    with engine.connect() as connection:
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        assert list(connection.execute(text("PRAGMA foreign_key_check"))) == []
    engine.dispose()

    command.upgrade(config, TARGET)
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO mold_tools "
                "(mold_code,mold_name,identity_status,version,rack_location,"
                "location_version,repair_status,repair_version,is_active,archive_status,label_overrides_json) "
                "VALUES ('ED0928-LOSS-GUARD','迁移保护模具','legacy_unset',1,"
                "'1F-M-R01-L1-G01',1,'normal',1,1,'active',:overrides)"
            ),
            {"overrides": '{"v":1,"remarks":"标签专用事实"}'},
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="保留数据库，使用支持当前标签配置的程序向前修复"):
        command.downgrade(config, PARENT)

    engine = create_engine(f"sqlite:///{database}")
    with engine.connect() as connection:
        assert connection.scalar(
            text(
                "SELECT label_overrides_json FROM mold_tools "
                "WHERE mold_code = 'ED0928-LOSS-GUARD'"
            )
        ) == '{"v":1,"remarks":"标签专用事实"}'
    engine.dispose()
