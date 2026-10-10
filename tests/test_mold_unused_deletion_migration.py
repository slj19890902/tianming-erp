from __future__ import annotations

from argparse import Namespace
import hashlib
from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest

ROOT = Path(__file__).parents[1]


def _facts(db):
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version'")]
    return {table: (tuple(r[1] for r in db.execute(f'PRAGMA table_info("{table}")')),
                    db.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()) for table in tables}


def _unchanged(db, before, objects):
    for table, (columns, rows) in before.items():
        quoted = ','.join(f'"{column}"' for column in columns)
        assert db.execute(f'SELECT {quoted} FROM "{table}" ORDER BY rowid').fetchall() == rows, table
    after = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE type IN ('index','trigger') AND sql IS NOT NULL"))
    assert all(after.get(name) == sql for name, sql in objects.items())
    assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_mold_deletion_migration_preserves_old_facts_and_refuses_loss(tmp_path, monkeypatch):
    path = tmp_path / 'deletion-migration.sqlite3'
    monkeypatch.setenv('ERP_DATABASE_PATH', str(path))
    config = Config(str(ROOT / 'alembic.ini'))
    config.set_main_option('script_location', str(ROOT / 'alembic'))
    config.cmd_opts = Namespace(x=[f'expected_database_path={path}'])
    assert ScriptDirectory.from_config(config).get_heads() == ['ep1010md']
    command.upgrade(config, 'eo1010pi')
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO users(username,password_hash,role,real_name,is_active,auth_version,must_change_password,customer_access_mode,ui_mode) VALUES ('test-delete','hash','admin','test',1,1,0,'all','standard')")
        db.execute("INSERT INTO mold_tools(mold_code,mold_name,identity_status,version,rack_location,location_version,repair_status,repair_version,is_active,archive_status) VALUES ('KEEP-ID','原始档案','legacy_unset',1,'R01',1,'normal',1,1,'active')")
        db.execute("INSERT INTO mold_master_mutations(action,idempotency_key,request_hash,mold_tool_id,actor_id,result_version,result_snapshot_json) VALUES ('create','migration-create-001',?,1,1,1,?)", ('a' * 64, '{"id":1}'))
        db.commit()
        before = _facts(db)
        objects = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE type IN ('index','trigger') AND sql IS NOT NULL"))
    for operation, target in ((command.upgrade, 'ep1010md'), (command.downgrade, 'eo1010pi'), (command.upgrade, 'ep1010md')):
        operation(config, target)
        with sqlite3.connect(path) as db:
            _unchanged(db, before, objects)
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("UPDATE mold_tools SET deleted_at='2026-10-10 01:00:00',deleted_by=1,delete_idempotency_key='migration-delete-001',is_active=0,version=2 WHERE id=1")
        db.commit()
        with pytest.raises(sqlite3.IntegrityError, match='immutable|invalid mold deletion state'):
            db.execute("UPDATE mold_tools SET deleted_at=NULL WHERE id=1")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(RuntimeError, match='禁止有损降级'):
        command.downgrade(config, 'eo1010pi')
    assert hashlib.sha256(path.read_bytes()).hexdigest() == sha
