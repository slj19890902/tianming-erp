from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


ROOT = Path(__file__).resolve().parents[1]
PARENT = "df40v8x9z29"
TARGET = "ef41v8x9z30"


def _config(monkeypatch: pytest.MonkeyPatch, database: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return config


def _trigger_names(engine) -> set[str]:
    with engine.connect() as connection:
        return {
            str(row[0])
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
            )
        }


def test_ef40_empty_upgrade_downgrade_upgrade_preserves_print_fact_guards(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "ef40-roundtrip.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, PARENT)
    command.upgrade(config, TARGET)
    engine = create_engine(f"sqlite:///{database}")
    columns = {column["name"] for column in inspect(engine).get_columns("mold_label_print_jobs")}
    assert {
        "label_layout_version",
        "label_layout_payload_json",
        "label_layout_payload_hash",
    }.issubset(columns)
    assert "mold_label_layout_revisions" in inspect(engine).get_table_names()
    trigger_names = _trigger_names(engine)
    assert "trg_mold_label_layout_revisions_immutable_update" in trigger_names
    assert "trg_mold_label_layout_revisions_immutable_delete" in trigger_names
    assert "trg_mold_label_print_jobs_layout_snapshot_insert" in trigger_names
    assert "trg_mold_label_print_jobs_immutable_update" in trigger_names
    assert "trg_mold_label_print_jobs_immutable_delete" in trigger_names

    engine.dispose()
    command.downgrade(config, PARENT)
    engine = create_engine(f"sqlite:///{database}")
    columns = {column["name"] for column in inspect(engine).get_columns("mold_label_print_jobs")}
    assert "label_layout_version" not in columns
    trigger_names = _trigger_names(engine)
    assert "trg_mold_label_print_jobs_immutable_update" in trigger_names
    assert "trg_mold_label_print_jobs_immutable_delete" in trigger_names
    with engine.connect() as connection:
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        assert list(connection.execute(text("PRAGMA foreign_key_check"))) == []
    engine.dispose()

    command.upgrade(config, TARGET)
    engine = create_engine(f"sqlite:///{database}")
    assert "mold_label_layout_revisions" in inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        assert list(connection.execute(text("PRAGMA foreign_key_check"))) == []
    engine.dispose()


def test_ef40_layout_revisions_are_immutable_and_block_destructive_downgrade(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from app.services.mold_label_layout import (
        CATALOG_VERSION,
        canonical_json,
        default_layout,
        layout_hash,
        normalize_layout,
    )

    database = tmp_path / "ef40-facts.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, TARGET)
    engine = create_engine(f"sqlite:///{database}")
    layout = normalize_layout(default_layout())
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO mold_label_layout_revisions "
                "(version,catalog_version,payload_json,payload_hash,operation_kind,"
                "operation_key,request_hash,source_release_version,created_by) "
                "VALUES (1,:catalog,:payload,:payload_hash,'save_and_publish',"
                "'p1-103-migration-fact',:request_hash,NULL,NULL)"
            ),
            {
                "catalog": CATALOG_VERSION,
                "payload": canonical_json(layout),
                "payload_hash": layout_hash(layout),
                "request_hash": "a" * 64,
            },
        )
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE mold_label_layout_revisions "
                    "SET operation_kind='restore_default' WHERE version=1"
                )
            )
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM mold_label_layout_revisions WHERE version=1")
            )
    engine.dispose()
    with pytest.raises(RuntimeError, match="layout facts exist"):
        command.downgrade(config, PARENT)
