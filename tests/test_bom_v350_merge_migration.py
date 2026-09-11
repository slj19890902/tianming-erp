"""Disposable v330 compatibility rehearsal; not fresh v350 data acceptance."""
import ast
import hashlib
from pathlib import Path
import sqlite3

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from tests.test_multilevel_bom_factory_compile import factory_copy
from tests.test_multilevel_bom_modes_migration import original_facts
from tests.test_p1_131_material_cost_lineage_migration import _config


def test_migration_identities_unique_and_formal_cost_lineage_preserved(monkeypatch, tmp_path):
    identities = {}
    for path in (Path(__file__).resolve().parents[1] / "alembic/versions").glob("*.py"):
        for node in ast.parse(path.read_text(encoding="utf-8-sig")).body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "revision" for t in node.targets):
                revision = ast.literal_eval(node.value)
                assert revision not in identities, (revision, path, identities.get(revision))
                identities[revision] = path.name
    scripts = ScriptDirectory.from_config(_config(monkeypatch, tmp_path / "unused.sqlite3"))
    assert scripts.get_heads() == ["so27v8x9z89"]
    assert scripts.get_revision("rx10v8x9z72").down_revision == "rw10v8x9z71"
    assert scripts.get_revision("rx11v8x9z72").down_revision == "rw09v8x9z71"
    assert scripts.get_revision("ry11v8x9z73").down_revision == "rx11v8x9z72"


def test_merge_keeps_original_rows_indexes_triggers_and_roundtrips(factory_copy, monkeypatch):
    target = Path(factory_copy.get_bind().url.database)
    original = target.with_name("before-upgrade.sqlite3")
    with sqlite3.connect(original) as db:
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                  if r[0] not in {"alembic_version", "sqlite_sequence"}]
        columns = {t: [r[1] for r in db.execute(f'PRAGMA table_info("{t}")')] for t in tables}
        expected = original_facts(db, columns)
        objects = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE type IN ('index','trigger') AND sql IS NOT NULL"))
    factory_copy.rollback()
    config = _config(monkeypatch, target)
    for destination in ("sn26v8x9z88", "head"):
        (command.downgrade if destination != "head" else command.upgrade)(config, destination)
        with sqlite3.connect(target) as db:
            assert original_facts(db, columns) == expected
            actual = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE type IN ('index','trigger') AND sql IS NOT NULL"))
            assert all(actual.get(key) == value for key, value in objects.items())
            assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
            expected_heads = {"sn26v8x9z88", "rz10v8x9z74"} if destination != "head" else {"so27v8x9z89"}
            assert {r[0] for r in db.execute("SELECT version_num FROM alembic_version")} == expected_heads


@pytest.mark.parametrize("kind", ["cost", "mail"])
def test_formal_facts_block_downgrade_before_file_changes(factory_copy, monkeypatch, kind):
    target = Path(factory_copy.get_bind().url.database)
    factory_copy.rollback()
    with sqlite3.connect(target) as db:
        db.execute("PRAGMA foreign_keys=ON")
        if kind == "cost":
            db.execute("INSERT INTO inventory_cost_rules(product_id,config_json,version,updated_by) "
                       "SELECT p.id,'{}',1,u.id FROM products p CROSS JOIN users u WHERE u.is_active=1 LIMIT 1")
        else:
            db.execute("INSERT INTO email_intake_settings(id,encrypted_secret,version) VALUES(1,'isolated-test-placeholder',1)")
    before = hashlib.sha256(target.read_bytes()).digest()
    with pytest.raises(RuntimeError, match="已有BOM、库存成本或邮件事实"):
        command.downgrade(_config(monkeypatch, target), "sn26v8x9z88")
    assert hashlib.sha256(target.read_bytes()).digest() == before
