import hashlib
from pathlib import Path
import sqlite3
import pytest
from sqlalchemy import select

from alembic import command
from alembic.script import ScriptDirectory

from tests.test_multilevel_bom_factory_compile import factory_copy
from tests.test_p1_131_material_cost_lineage_migration import _config


def test_new_configuration_blocks_downgrade_without_losing_data(factory_copy, monkeypatch):
    from app.models.product import Product
    from app.models.user import User
    from app.services.composite_bom import replace_product_bom
    db = factory_copy
    actor = db.scalar(select(User).where(User.role == "admin", User.is_active.is_(True)))
    product = db.get(Product, 3799)
    replace_product_bom(db, parent_product_id=product.id, expected_version=product.version,
        user=actor, inventory_mode="separate", material_mode="expand_children", delivery_mode="components",
        components=[dict(component_product_id=3771, quantity_per_set=3, inventory_relation="accompany"),
                    dict(component_product_id=3783, quantity_per_set=4, inventory_relation="accompany")])
    db.commit()
    target = Path(db.get_bind().url.database)
    config = _config(monkeypatch, target)
    command.downgrade(config, "sf18v8x9z80")
    before = hashlib.sha256(target.read_bytes()).hexdigest()
    with pytest.raises(RuntimeError, match="已有独立BOM配置"):
        command.downgrade(config, "se17v8x9z79")
    assert hashlib.sha256(target.read_bytes()).hexdigest() == before


def original_facts(connection, columns):
    result = {}
    for table, fields in columns.items():
        select_fields = ",".join('"' + f.replace('"', '""') + '"' for f in fields)
        rows = connection.execute(f'SELECT {select_fields} FROM "{table}"').fetchall()
        result[table] = hashlib.sha256(repr(sorted(rows, key=repr)).encode()).hexdigest()
    return result


def test_stock_identity_facts_block_destructive_downgrade(factory_copy, monkeypatch):
    db = factory_copy
    target = Path(db.get_bind().url.database)
    db.rollback()
    # Isolate the 82 gate from later empty schema-only revisions.
    command.downgrade(_config(monkeypatch, target), "sh20v8x9z82")
    with sqlite3.connect(target) as connection:
        changed = connection.execute("UPDATE finished_goods_inventory_details SET physical_basis_json='{}' "
            "WHERE inventory_lot_id=(SELECT MIN(inventory_lot_id) FROM finished_goods_inventory_details)")
        assert changed.rowcount == 1
    before = hashlib.sha256(target.read_bytes()).hexdigest()
    with pytest.raises(RuntimeError, match="已有库存规格工艺身份依据"):
        command.downgrade(_config(monkeypatch, target), "sg19v8x9z81")
    assert hashlib.sha256(target.read_bytes()).hexdigest() == before


def test_factory_copy_upgrade_roundtrip_preserves_every_original_fact(factory_copy, monkeypatch):
    db = factory_copy
    target = Path(db.get_bind().url.database)
    backup = target.with_name("before-upgrade.sqlite3")
    with sqlite3.connect(backup.as_uri() + "?mode=ro", uri=True) as before:
        tables = [r[0] for r in before.execute("SELECT name FROM sqlite_master WHERE type='table'")
                  if r[0] not in {"alembic_version", "sqlite_sequence"}]
        columns = {t: [r[1] for r in before.execute(f'PRAGMA table_info("{t}")')] for t in tables}
        expected = original_facts(before, columns)
        triggers = dict(before.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'"))
    db.rollback()
    config = _config(monkeypatch, target)
    assert ScriptDirectory.from_config(config).get_heads() == ["sk23v8x9z85"]
    for destination in ("sf18v8x9z80", "se17v8x9z79", "sk23v8x9z85"):
        if destination != "sk23v8x9z85":
            command.downgrade(config, destination)
        else:
            command.upgrade(config, destination)
        with sqlite3.connect(target) as after:
            assert after.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert after.execute("PRAGMA foreign_key_check").fetchall() == []
            expected_heads = {destination} if destination == "sk23v8x9z85" else {destination, "rv10v8x9z70"}
            assert {r[0] for r in after.execute("SELECT version_num FROM alembic_version")} == expected_heads
            assert original_facts(after, columns) == expected
            actual = dict(after.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'"))
            assert all(actual.get(name) == sql for name, sql in triggers.items())
