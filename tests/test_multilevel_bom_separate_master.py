import json

import pytest
from sqlalchemy import select

from app.models.audit import OperationLog
from app.models.product import Product
from app.services.composite_bom import CompositeBOMError, get_product_bom, replace_product_bom
from app.services.multilevel_bom_orders import freeze_master_order_bom, read_compiled_order_bom
from app.services.multilevel_bom_plan import plan_bom
from tests.test_multilevel_bom_orders import context


def configure(db, actor, **overrides):
    args = dict(parent_product_id=1, expected_version=db.get(Product, 1).version,
        user=actor, inventory_mode="separate", material_mode="expand_children", delivery_mode="components",
        components=[dict(component_product_id=3, quantity_per_set=3, inventory_relation="accompany"),
                    dict(component_product_id=4, quantity_per_set=4, inventory_relation="accompany")])
    return replace_product_bom(db, **(args | overrides))


def test_save_reopen_and_new_order_freeze_keep_independent_modes(context):
    db, actor, item, _ = context
    result = configure(db, actor)
    db.commit()
    assert get_product_bom(db, 1) == result
    assert db.get(Product, 1).is_virtual_composite_parent
    freeze_master_order_bom(db, order_item_id=item.id, actor=actor)
    db.commit()
    old = read_compiled_order_bom(db, item.id)
    assert old.graph.modes.delivery == "components"
    assert dict(plan_bom(old.graph, 100).picking) == {3: 300, 4: 400}
    changed = configure(db, actor, delivery_mode="parent")
    db.commit()
    assert changed["version"] == result["version"] + 1
    assert changed["delivery_mode"] == "parent"
    assert read_compiled_order_bom(db, item.id).graph == old.graph
    logs = list(db.scalars(select(OperationLog).where(OperationLog.action == "BOM_UPDATE").order_by(OperationLog.id)))
    assert json.loads(logs[-1].details)["before_delivery_mode"] == "components"
    assert json.loads(logs[-1].details)["after_delivery_mode"] == "parent"


def test_failed_mode_change_rolls_back_children_and_version(context):
    db, actor, _, _ = context
    saved = configure(db, actor)
    db.commit()
    with pytest.raises(CompositeBOMError):
        configure(db, actor, inventory_mode="assembled")
    db.commit()
    assert get_product_bom(db, 1) == saved


def test_child_unit_is_real_and_incompatible_input_rolls_back(context):
    db, actor, _, _ = context
    saved = configure(db, actor)
    db.commit()
    assert saved["components"][0]["component"]["unit"] == db.get(Product, 3).unit
    rows = [dict(component_product_id=3, quantity_per_set=3, inventory_relation="accompany", unit="不存在的换算单位"),
            dict(component_product_id=4, quantity_per_set=4, inventory_relation="accompany")]
    with pytest.raises(CompositeBOMError, match="单位与真实产品单位"):
        configure(db, actor, components=rows)
    db.commit()
    assert get_product_bom(db, 1) == saved
