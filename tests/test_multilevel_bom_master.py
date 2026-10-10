import pytest
from sqlalchemy import select, func

from app.models.product import Product
from app.models.product_bom import ProductBomComponent
from app.models.multilevel_bom import ProductBomProfile, ProductBomInventoryRelation
from app.services.composite_bom import replace_product_bom, get_product_bom, CompositeBOMError
from app.services.multilevel_bom_master import preview_master_structure
from tests.test_multilevel_bom_orders import context


def save(db, actor, pid, mode, rows, version=None):
    return replace_product_bom(db, parent_product_id=pid, inventory_mode=mode,
        components=[{"component_product_id": child, "quantity_per_set": quantity,
                     "inventory_relation": relation} for child, quantity, relation in rows],
        expected_version=version if version is not None else db.get(Product, pid).version, user=actor)


def configure_liner(db, actor):
    save(db, actor, 2, "assembled", [(3, 2, "assembly"), (4, 6, "assembly")])
    return save(db, actor, 1, "manufactured", [(2, 1, "accompany")])


def test_real_liner_is_parent_of_long_and_short_and_child_of_carton(context):
    db, actor, _, _ = context
    response = configure_liner(db, actor)
    db.commit()
    assert response["components"][0]["component_product_id"] == 2
    assert response["components"][0]["inventory_relation"] == "accompany"
    assert db.get(Product, 2).is_composite is True
    assert db.get(Product, 2).is_internal_component is True
    structure = preview_master_structure(db, 1)
    assert {(e["parent_id"], e["child_id"], e["quantity"], e["relation"]) for e in structure["edges"]} == {
        (1, 2, 1, "accompany"), (2, 3, 2, "assembly"), (2, 4, 6, "assembly")}
    assert {node["code"] for node in structure["nodes"]} == {"SHARED-CODE"}
    assert len(structure["nodes"]) == 4


def test_internal_liner_can_be_edited_without_breaking_parent_identity(context):
    db, actor, _, _ = context
    configure_liner(db, actor)
    db.commit()
    save(db, actor, 2, "assembled", [(3, 3, "assembly"), (4, 6, "assembly")])
    db.commit()
    assert get_product_bom(db, 1)["components"][0]["component_product_id"] == 2
    assert get_product_bom(db, 2)["components"][0]["quantity_per_set"] == 3


def test_source_only_change_is_versioned_and_read_back(context):
    db, actor, _, _ = context
    before = db.get(Product, 1).version
    response = save(db, actor, 1, "manufactured", [])
    db.commit()
    assert response["version"] == before + 1
    assert response["inventory_mode"] == "manufactured"
    again = save(db, actor, 1, "manufactured", [])
    assert again["version"] == response["version"]


def test_relation_only_change_is_not_silently_ignored(context):
    db, actor, _, _ = context
    first = save(db, actor, 1, "assembled", [(3, 2, "assembly"), (4, 6, "assembly")])
    db.commit()
    second = save(db, actor, 1, "assembled", [(3, 2, "assembly"), (4, 6, "accompany")])
    db.commit()
    assert second["version"] == first["version"] + 1
    assert second["components"][1]["inventory_relation"] == "accompany"


def test_stale_version_does_not_change_profile_or_edges(context):
    db, actor, _, _ = context
    configure_liner(db, actor)
    db.commit()
    with pytest.raises(CompositeBOMError, match="版本已变化"):
        save(db, actor, 2, "assembled", [(3, 9, "assembly")], version=1)
    db.commit()
    assert [r["quantity_per_set"] for r in get_product_bom(db, 2)["components"]] == [2, 6]


def test_cycle_and_legacy_parent_cannot_be_bypassed(context):
    db, actor, _, _ = context
    configure_liner(db, actor)
    db.commit()
    with pytest.raises(CompositeBOMError, match="循环引用"):
        save(db, actor, 2, "assembled", [(1, 1, "assembly")])
    assert len(get_product_bom(db, 2)["components"]) == 2


@pytest.mark.parametrize("mode,rows", [
    ("assembled", []),
    ("assembled", [(3, 1, "accompany")]),
    ("assembled", [(3, 1, "unknown")]),
])
def test_invalid_source_or_relationship_is_rejected_before_writes(context, mode, rows):
    db, actor, _, _ = context
    with pytest.raises(CompositeBOMError):
        save(db, actor, 1, mode, rows)
    db.commit()
    assert db.get(ProductBomProfile, 1) is None
    assert db.scalar(select(func.count()).select_from(ProductBomComponent)) == 0


def test_manufactured_body_can_assemble_real_children(context):
    db, actor, _, _ = context
    save(db, actor, 1, "manufactured", [(3, 2, "assembly"), (4, 1, "accompany")])
    db.commit()
    from app.services.multilevel_bom_master import load_master_structure
    structure = load_master_structure(db, 1)
    assert structure["profiles"][1] == "manufactured"
    assert {(e["child_id"],e["quantity"],e["relation"]) for e in structure["edges"]} == {(3,2,"assembly"),(4,1,"accompany")}


def test_failed_deep_validation_rolls_back_all_master_changes(context):
    db, actor, _, _ = context
    save(db, actor, 2, "assembled", [(3, 2, "assembly"), (4, 6, "assembly")])
    db.commit()
    db.get(Product, 4).is_active = False
    db.commit()
    version = db.get(Product, 1).version
    with pytest.raises(CompositeBOMError, match="失效产品"):
        save(db, actor, 1, "manufactured", [(2, 1, "accompany")])
    db.commit()
    assert db.get(Product, 1).version == version
    assert db.get(ProductBomProfile, 1) is None
    assert get_product_bom(db, 1)["components"] == []


def test_outer_cancel_does_not_save_profile_or_real_bom_edges(context):
    db, actor, _, _ = context
    configure_liner(db, actor)
    db.rollback()
    assert db.scalar(select(func.count()).select_from(ProductBomProfile)) == 0
    assert db.scalar(select(func.count()).select_from(ProductBomComponent)) == 0
    assert db.scalar(select(func.count()).select_from(ProductBomInventoryRelation)) == 0


def test_assembled_product_unit_is_sets_not_child_pieces(context):
    db, actor, _, _ = context
    save(db, actor, 1, "assembled", [(3, 3, "assembly"), (4, 4, "assembly")])
    db.commit()
    assert db.get(Product, 1).unit == "套"
    assert db.get(Product, 1).is_virtual_composite_parent is False


def test_api_save_reload_and_structure_use_real_ids(context):
    from app.api.products import ProductBOMUpdatePayload, update_product_bom, read_product_bom_structure
    db, actor, _, _ = context
    child = ProductBOMUpdatePayload(expected_version=1, inventory_mode="assembled", components=[
        {"component_product_id": 3, "quantity_per_set": 2, "inventory_relation": "assembly"},
        {"component_product_id": 4, "quantity_per_set": 6, "inventory_relation": "assembly"}])
    update_product_bom(2, child, db=db, user=actor)
    parent = ProductBOMUpdatePayload(expected_version=1, inventory_mode="manufactured", components=[
        {"component_product_id": 2, "quantity_per_set": 1, "inventory_relation": "accompany"}])
    response = update_product_bom(1, parent, db=db, user=actor)
    assert response["inventory_mode"] == "manufactured"
    assert len(read_product_bom_structure(1, db=db, user=actor)["nodes"]) == 4


def test_structure_api_keeps_customer_scope(context):
    from fastapi import HTTPException
    from app.api.products import read_product_bom_structure
    db, actor, _, _ = context
    configure_liner(db, actor)
    db.commit()
    actor.role = "sales"
    actor.customer_access_mode = "selected"
    db.commit()
    with pytest.raises(HTTPException) as error:
        read_product_bom_structure(1, db=db, user=actor)
    assert error.value.status_code == 403


def test_equal_sheet_sizes_keep_two_real_molds_and_independent_yields(context):
    from app.models.mold_tool import MoldTool, MoldToolCustomer
    from app.api.products import read_product_bom
    from app.services.sheet_cutting_settings import theoretical_product_yield
    db, actor, _, _ = context
    rows = []
    for child_id, label, mold_count in [(3, "A模", 2), (4, "B模", 4)]:
        mold = MoldTool(mold_code=f"UAT-AB-{child_id}", mold_name=label, label_name=label,
                        identity_status="frozen", rack_location=f"1F-M-R0{child_id}-L2")
        db.add(mold)
        db.flush()
        child = db.get(Product, child_id)
        db.add(MoldToolCustomer(mold_tool_id=mold.id, customer_id=child.customer_id))
        child.box_category = "die_cut"
        child.production_process = "模切"
        child.report_length_mm, child.report_width_mm = 800, 600
        child.sheet_cutting_settings = {"schema_version": 2, "whole": {
            "length_parts": 1, "width_parts": 1, "mold_count": mold_count, "is_die_cut": True}}
        child.mold_tool_id = mold.id
        rows.append({"component_product_id": child_id, "quantity_per_set": 1,
                     "inventory_relation": "assembly", "is_die_cut": True, "mold_tool_id": mold.id})
    replace_product_bom(db, parent_product_id=1, inventory_mode="assembled", components=rows,
                        expected_version=1, user=actor)
    db.commit()
    result = read_product_bom(1, db=db, user=actor)
    assert len(result["components"]) == 2
    assert len({row["mold_tool_id"] for row in result["components"]}) == 2
    assert [row["component"]["report_length_mm"] for row in result["components"]] == [800, 800]
    assert [row["component"]["sheet_cutting_settings"]["whole"]["mold_count"] for row in result["components"]] == [2, 4]
    assert [theoretical_product_yield(db.get(Product, pid)) for pid in (3, 4)] == [2, 4]
