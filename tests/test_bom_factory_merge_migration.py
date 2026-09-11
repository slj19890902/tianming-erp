"""Rehearse from the populated factory v319 copy, never from the live DB."""
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from tests.test_p1_131_material_cost_lineage_migration import _config


def test_factory_supplements_survive_bom_merge_and_round_trip(monkeypatch, tmp_path):
    source = Path(os.environ.get("ERP_MULTILEVEL_UAT_SOURCE", "")).resolve()
    if source.name != "order-graph-source-isolated.sqlite3" or "tm-uat" not in source.parts:
        pytest.skip("explicit isolated factory source required")
    digest = hashlib.sha256(source.read_bytes()).digest()
    target, backup = tmp_path / "merge.sqlite3", tmp_path / "before-upgrade.sqlite3"
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src:
        if src.execute("SELECT version_num FROM alembic_version").fetchall() != [("ru10v8x9z69",)]:
            pytest.skip("requires populated factory v319 source")
        with sqlite3.connect(target) as dest:
            src.backup(dest)
    shutil.copy2(target, backup)
    assert hashlib.sha256(backup.read_bytes()).digest() == hashlib.sha256(target.read_bytes()).digest()
    with sqlite3.connect(backup) as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name!='alembic_version'")]
        columns = {table: [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')] for table in tables}
        assert db.execute("SELECT count(*) FROM finance_material_cost_supplements").fetchone()[0] > 0

    def facts():
        with sqlite3.connect(target) as db:
            assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
            hashes = {}
            for table, names in columns.items():
                selected = ",".join('"' + name + '"' for name in names)
                rows = sorted(repr(tuple(row)) for row in db.execute(f'SELECT {selected} FROM "{table}"'))
                hashes[table] = hashlib.sha256("\n".join(rows).encode()).hexdigest()
            triggers = db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name").fetchall()
            return hashes, triggers

    before = facts()
    config = _config(monkeypatch, target)
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == ["se17v8x9z79"]
    assert set(scripts.get_revision("se17v8x9z79").down_revision) == {"sd16v8x9z78", "ru10v8x9z69"}
    command.upgrade(config, "head")
    assert facts() == before
    # Undo only the join, retaining BOTH branches and all approved factory rows.
    command.downgrade(config, "sd16v8x9z78")
    with sqlite3.connect(target) as db:
        assert set(db.execute("SELECT version_num FROM alembic_version").fetchall()) == {("sd16v8x9z78",), ("ru10v8x9z69",)}
    assert facts() == before
    command.upgrade(config, "head")
    assert facts() == before
    with sqlite3.connect(target) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchall() == [("se17v8x9z79",)]
        for sql in ["UPDATE finance_material_cost_supplements SET reason=reason", "DELETE FROM finance_material_cost_supplements"]:
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(sql)
            db.rollback()
    assert facts() == before
    assert hashlib.sha256(source.read_bytes()).digest() == digest
