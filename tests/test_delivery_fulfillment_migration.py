"""Run the real revision against owned SQLite, including failed DDL rollback."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
import pytest

from app.models.delivery_fulfillment_fact import FULFILLMENT_GUARD_NAMES
from app.models.user import User
from app.services.delivery_fulfillment_facts import append_delivery_fulfillment
from tests.test_delivery_fulfillment_facts import fulfillment_db, _submit_payload

ROOT = Path(__file__).resolve().parents[1]
TARGET = "fb1023df"
BASE = "fa1023oc"


def _migration():
    spec = importlib.util.spec_from_file_location("fulfillment_migration", ROOT / "alembic/versions/fb1023df_delivery_fulfillment_facts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _snapshot(path):
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name!='alembic_version' ORDER BY name")]
        facts = {}
        for table in tables:
            rows = db.execute('SELECT * FROM "' + table + '" ORDER BY rowid').fetchall()
            facts[table] = hashlib.sha256(json.dumps(rows, default=str, ensure_ascii=False).encode()).hexdigest()
        schema = db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
        return dict(facts=facts, schema=schema)


@pytest.fixture
def legacy_fulfillment_db(fulfillment_db, monkeypatch):
    engine = fulfillment_db.kw["bind"]
    path = Path(engine.url.database)
    migration = _migration()
    # Existing synthetic commercial/stock facts are preserved; remove only the
    # empty new tables to create an owned pre-migration fixture.
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        connection.exec_driver_sql("CREATE TABLE alembic_version(version_num VARCHAR(32) NOT NULL PRIMARY KEY)")
        connection.exec_driver_sql("INSERT INTO alembic_version VALUES ('fa1023oc')")
    monkeypatch.setenv("ERP_ENVIRONMENT", "test")
    monkeypatch.setenv("ERP_DATABASE_PATH", str(path))
    monkeypatch.setenv("ERP_SECRET_KEY", "isolated-fulfillment-migration-test-secret")
    config = Config(str(ROOT / "alembic.ini"))
    config.cmd_opts = SimpleNamespace(x=["expected_database_path=" + str(path)])
    return fulfillment_db, path, config


def test_real_revision_roundtrip_preserves_all_old_facts_and_schema(legacy_fulfillment_db):
    _, path, config = legacy_fulfillment_db
    assert ScriptDirectory.from_config(config).get_heads() == [TARGET]
    before = _snapshot(path)
    for action, revision in ((command.upgrade, TARGET), (command.downgrade, BASE), (command.upgrade, TARGET)):
        action(config, revision)
        after = _snapshot(path)
        assert {t: after["facts"][t] for t in before["facts"]} == before["facts"]
        assert all(row in after["schema"] for row in before["schema"])
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (revision,)
            assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert not db.execute("PRAGMA foreign_key_check").fetchall()
            if revision == TARGET:
                assert FULFILLMENT_GUARD_NAMES <= {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
            else:
                assert after == before


def test_real_facts_refuse_downgrade_before_any_write(legacy_fulfillment_db):
    factory, path, config = legacy_fulfillment_db
    command.upgrade(config, TARGET)
    with factory() as db:
        actor = db.get(User, 1)
        result = append_delivery_fulfillment(db, actor, 142, _submit_payload(db, actor))
        db.commit()
        assert result["revision"] == 1
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="refuse lossy downgrade"):
        command.downgrade(config, BASE)
    assert path.read_bytes() == before
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (TARGET,)
        assert db.execute("SELECT count(*) FROM delivery_fulfillment_segments").fetchone() == (3,)
        assert not db.execute("PRAGMA foreign_key_check").fetchall()


def test_unsealed_segment_alone_also_refuses_lossy_downgrade(legacy_fulfillment_db):
    _, path, config = legacy_fulfillment_db
    command.upgrade(config, TARGET)
    # Simulate a damaged standalone segment only in this disposable fixture.
    # Downgrade must preserve even invalid facts for diagnosis, not delete them.
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("INSERT INTO delivery_fulfillment_segments(revision_id,delivery_item_id,actual_date,quantity,evidence_json) VALUES (999,817,'2026-09-22',50,'{}')")
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="refuse lossy downgrade"):
        command.downgrade(config, BASE)
    assert path.read_bytes() == before


def test_late_upgrade_failure_rolls_back_all_new_ddl(legacy_fulfillment_db):
    factory, path, _ = legacy_fulfillment_db
    before = path.read_bytes()
    migration = _migration()
    with pytest.raises(RuntimeError, match="isolated late DDL failure"):
        with factory.kw["bind"].begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                with patch.object(migration, "_install_frozen_guards", side_effect=RuntimeError("isolated late DDL failure")):
                    migration.upgrade()
    assert path.read_bytes() == before


def test_frozen_migration_guards_match_current_model_contract(legacy_fulfillment_db):
    _, path, config = legacy_fulfillment_db
    command.upgrade(config, TARGET)
    from sqlalchemy import create_engine
    from app.models.delivery_fulfillment_fact import install_delivery_fulfillment_guards
    engine = create_engine("sqlite://")
    # Compare normalized SQL from the independently frozen revision, not an
    # import of application guard code inside the migration itself.
    with sqlite3.connect(path) as db:
        frozen = {r[0]: " ".join(r[1].split()) for r in db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'") if r[0] in FULFILLMENT_GUARD_NAMES}
    with engine.begin() as connection:
        for table in ("delivery_fulfillment_revisions", "delivery_fulfillment_segments", "sales_deliveries", "sales_delivery_items"):
            connection.exec_driver_sql(f"CREATE TABLE {table}(id INTEGER)")
        install_delivery_fulfillment_guards(connection)
        model = {r[0]: " ".join(r[1].split()) for r in connection.exec_driver_sql("SELECT name,sql FROM sqlite_master WHERE type='trigger'")}
        assert frozen == model
    engine.dispose()
