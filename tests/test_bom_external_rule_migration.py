import hashlib
from pathlib import Path
import sqlite3

import pytest
from alembic import command
from sqlalchemy import select

from app.models.user import User
from app.models.multilevel_bom import OrderBomExternalComponent
from app.models.product_bom import SalesOrderItemBomComponent
from app.models.order_external_packaging import SalesOrderItemExternalComponent
from app.services.composite_bom import get_product_bom
from app.services.multilevel_bom_external_freeze import freeze_order_procurement
from tests.test_multilevel_bom_factory_compile import factory_copy, new_item
from tests.test_multilevel_bom_master import save
from tests.test_multilevel_bom_modes_migration import original_facts
from tests.test_p1_131_material_cost_lineage_migration import _config


def test_external_migration_preserves_real_sources_and_rejects_cross_identity(factory_copy, monkeypatch):
    db = factory_copy
    actor = db.scalar(select(User).where(User.role == "admin", User.is_active.is_(True)))
    rows = [(r["component_product_id"], int(r["quantity_per_set"]), "accompany")
        for r in get_product_bom(db, 3479)["components"]]
    save(db, actor, 3479, "manufactured", rows)
    item = new_item(db, 3479, 2)
    freeze_order_procurement(db, order_item_id=item.id, actor=actor)
    db.commit()
    link = db.scalar(select(OrderBomExternalComponent).where(OrderBomExternalComponent.order_item_id == item.id))
    assert link is not None
    source = db.get(SalesOrderItemBomComponent, link.bom_snapshot_id)
    component = db.get(SalesOrderItemExternalComponent, link.external_component_id)
    source_values = {c.name: getattr(source, c.name) for c in source.__table__.columns if c.name != "id"}
    component_values = {c.name: getattr(component, c.name) for c in component.__table__.columns if c.name != "id"}
    owner, product_id = item.id, link.product_id
    db.rollback()
    path = Path(db.get_bind().url.database)
    config = _config(monkeypatch, path)
    command.downgrade(config, "si21v8x9z83")
    with sqlite3.connect(path) as before:
        tables = [row[0] for row in before.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if row[0] not in {"alembic_version", "sqlite_sequence"}]
        columns = {table: [row[1] for row in before.execute(f'PRAGMA table_info("{table}")')] for table in tables}
        facts = original_facts(before, columns)
    command.upgrade(config, "sj22v8x9z84")
    with sqlite3.connect(path) as after:
        assert original_facts(after, columns) == facts
        assert after.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert after.execute("PRAGMA foreign_key_check").fetchall() == []
    source_values.update(display_order=source_values["display_order"] + 1000, product_bom_component_id=None)
    component_values["display_order"] += 1000
    second_source = SalesOrderItemBomComponent(**source_values)
    second_component = SalesOrderItemExternalComponent(**component_values)
    db.add_all([second_source, second_component])
    db.flush()
    new_ids = second_component.id, second_source.id
    db.commit()
    with sqlite3.connect(path) as raw:
        raw.execute("PRAGMA foreign_keys=ON")
        statement = "INSERT INTO order_bom_external_components(external_component_id,order_item_id,product_id,bom_snapshot_id) VALUES(?,?,?,?)"
        for values in ((new_ids[0], owner, 3479, new_ids[1]), (new_ids[0], owner + 99999, product_id, new_ids[1])):
            with pytest.raises(sqlite3.IntegrityError):
                raw.execute(statement, values)
        raw.execute(statement, (new_ids[0], owner, product_id, new_ids[1]))
        raw.commit()
    before_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(RuntimeError, match="多个外购冻结来源"):
        command.downgrade(config, "si21v8x9z83")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before_hash
