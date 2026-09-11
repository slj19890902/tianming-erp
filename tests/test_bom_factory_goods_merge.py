"""Retain factory applicability facts while joining the independent BOM head."""
from pathlib import Path
import hashlib
import sqlite3
import pytest

from alembic import command
from alembic.script import ScriptDirectory

from tests.test_multilevel_bom_factory_compile import factory_copy
from tests.test_multilevel_bom_modes_migration import original_facts
from tests.test_p1_131_material_cost_lineage_migration import _config


def test_merge_roundtrip_keeps_factory_usage_and_bom_tables(factory_copy, monkeypatch):
    db = factory_copy
    path = Path(db.get_bind().url.database)
    db.rollback()
    config = _config(monkeypatch, path)
    assert ScriptDirectory.from_config(config).get_heads() == ["sn26v8x9z88"]
    # This test owns the factory merge at 86; the additive handoff schema has
    # its own roundtrip and immutable-fact failure checks.
    command.downgrade(config, "sl24v8x9z86")
    with sqlite3.connect(path) as seed:
        lot = seed.execute("SELECT id FROM inventory_lots ORDER BY id LIMIT 1").fetchone()[0]
        seed.execute("INSERT INTO warehouse_goods_profiles(lot_id,data_json) VALUES(?,?)",
            (lot, '{"isolated_migration_proof":true}'))
        seed.execute("UPDATE materials SET is_white_face=1 WHERE id=(SELECT min(id) FROM materials)")
        seed.commit()
        columns = {t: [r[1] for r in seed.execute(f'PRAGMA table_info("{t}")')]
            for (t,) in seed.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if t not in {"alembic_version", "sqlite_sequence"}}
        expected = original_facts(seed, columns)
    command.downgrade(config, "sj22v8x9z84")
    with sqlite3.connect(path) as check:
        assert {r[0] for r in check.execute("SELECT version_num FROM alembic_version")} == {"sj22v8x9z84", "rw10v8x9z71"}
        assert original_facts(check, columns) == expected
    command.upgrade(config, "sl24v8x9z86")
    with sqlite3.connect(path) as check:
        assert check.execute("SELECT version_num FROM alembic_version").fetchall() == [("sl24v8x9z86",)]
        assert original_facts(check, columns) == expected
        assert check.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert check.execute("PRAGMA foreign_key_check").fetchall() == []


def test_paper_color_facts_refuse_downgrade_before_any_metadata_write(factory_copy, monkeypatch):
    db = factory_copy
    path = Path(db.get_bind().url.database)
    db.rollback()
    command.downgrade(_config(monkeypatch, path), "sl24v8x9z86")
    with sqlite3.connect(path) as seed:
        assert seed.execute("SELECT count(*) FROM supplier_paper_codes").fetchone()[0] > 0
        seed.execute("UPDATE supplier_paper_codes SET color='white' WHERE id=(SELECT min(id) FROM supplier_paper_codes)")
        seed.commit()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(RuntimeError, match="纸种颜色事实"):
        command.downgrade(_config(monkeypatch, path), "sk23v8x9z85")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    with sqlite3.connect(path) as check:
        assert check.execute("SELECT version_num FROM alembic_version").fetchall() == [("sl24v8x9z86",)]
        assert check.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert check.execute("PRAGMA foreign_key_check").fetchall() == []
