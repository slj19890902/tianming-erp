"""Synthetic BOM drawing fixtures; never write or infer production data."""
import json
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base
from app.models.customer import Customer
from app.models.drawing_design import DrawingRelease
from app.models.product import Product
from app.models.product_bom import ProductBomComponent
from app.services.drawing_components import component_context, validate_component_placements
from app.services.drawing_geometry import build_geometry


@pytest.fixture
def assembly_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'drawing-components.sqlite3'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        c = Customer(name="隔离图纸组合客户")
        other = Customer(name="隔离其他客户")
        db.add_all([c, other]); db.flush()
        products = []
        for name, style in [("纸盒本体", "模切内盒"), ("组合内衬", "BOM组合"),
                            ("长隔板", "隔板"), ("短隔板", "隔板")]:
            p = Product(customer_id=c.id, product_code="SAME-CUSTOMER-CODE",
                        customer_material_code="SAME-CUSTOMER-CODE",
                        product_name=name, box_style=style, version=1,
                        length_mm=100, width_mm=50, is_active=True)
            db.add(p); db.flush(); products.append(p)
        root, inner, long, short = products
        rows = []
        for parent, child, quantity, order in [(root, inner, 1, 1), (inner, long, 2, 1), (inner, short, 6, 2)]:
            row = ProductBomComponent(parent_product_id=parent.id, component_product_id=child.id,
                       quantity_per_set=quantity, display_order=order,
                       internal_component_code=f"part-{child.id}", is_die_cut=False)
            db.add(row); db.flush(); rows.append(row)
        for p in (long, short):
            g = build_geometry("liner_v1", {"length_mm": 100, "width_mm": 50})
            db.add(DrawingRelease(product_id=p.id, customer_id=c.id, revision="R1",
                       external_number=f"drawing-{p.id}", internal_number=f"internal-{p.id}",
                       idempotency_key=f"synthetic-{p.id}", design_version=1, product_version=1,
                       template_key="liner_v1", manifest_json=json.dumps({"geometry": g,"fold_model":[]}),
                       pdf_reference="test-unused", pdf_sha256="0"*64))
        db.commit()
        yield db, products, rows, other
    engine.dispose()


def placements_for(context):
    nodes = {n["product_id"]: n for n in context["nodes"]}
    result = []
    for instance in context["instances"]:
        node = nodes[instance["product_id"]]
        if not node["has_body"]:
            continue
        for index in range(instance["instance_count"]):
            result.append({"path": instance["path"], "instance_index": index,
                           "child_release_id": node["latest_release"]["id"],
                           "position_mm": [index*10,0,0], "rotation_deg": [90,0,0]})
    return result


def test_real_identity_quantity_and_no_business_write(assembly_db):
    db, (root, inner, long, short), rows, _ = assembly_db
    before = [(r.id, r.quantity_per_set, r.mold_tool_id) for r in rows]
    context = component_context(db, root)
    assert len({n["product_id"] for n in context["nodes"]}) == 4
    assert [i["instance_count"] for i in context["instances"]] == [1, 2, 6]
    assert not next(n for n in context["nodes"] if n["product_id"] == inner.id)["has_body"]
    payload = placements_for(context)
    result = validate_component_placements(db, root, payload, context["basis_hash"], for_publish=True)
    assert len(result) == 8
    assert result[0]["geometry"]["width_mm"] == "50"
    assert result[0]["position_mm"] == ["0", "0", "0"]
    assert not db.new and not db.dirty and not db.deleted
    assert before == [(r.id, r.quantity_per_set, r.mold_tool_id) for r in rows]


def test_partial_draft_allowed_but_publish_requires_actual_counts(assembly_db):
    db, products, _, _ = assembly_db
    context = component_context(db, products[0]); payload = placements_for(context)[:1]
    assert len(validate_component_placements(db, products[0], payload)) == 1
    with pytest.raises(HTTPException, match="") as caught:
        validate_component_placements(db, products[0], payload, for_publish=True)
    assert caught.value.status_code == 422
    assert "未摆放" in caught.value.detail


@pytest.mark.parametrize("mutation", ["wrong_product", "wrong_index", "duplicate", "nan", "boolean", "wrong_path"])
def test_forged_or_invalid_placement_rejected(assembly_db, mutation):
    db, products, _, _ = assembly_db
    context = component_context(db, products[0]); payload = placements_for(context)
    if mutation == "wrong_product": payload[0]["child_release_id"] = payload[-1]["child_release_id"]
    if mutation == "wrong_index": payload[0]["instance_index"] = 2
    if mutation == "duplicate": payload.append(dict(payload[0]))
    if mutation == "nan": payload[0]["position_mm"] = ["NaN",0,0]
    if mutation == "boolean": payload[0]["rotation_deg"] = [True,0,0]
    if mutation == "wrong_path": payload[0]["path"] = "missing-edge"
    with pytest.raises(HTTPException) as caught:
        validate_component_placements(db, products[0], payload)
    assert caught.value.status_code == 422


def test_bom_change_conflicts_and_old_release_is_not_rebuilt(assembly_db):
    db, products, rows, _ = assembly_db
    context = component_context(db, products[0]); payload = placements_for(context)
    products[2].length_mm = Decimal("180")
    products[2].version += 1
    db.commit()
    with pytest.raises(HTTPException) as caught:
        validate_component_placements(db, products[0], payload, context["basis_hash"])
    assert caught.value.status_code == 409
    updated = component_context(db, products[0])
    result = validate_component_placements(db, products[0], payload, updated["basis_hash"])
    assert result[0]["geometry"]["height_mm"] == "100"


def test_cross_customer_and_cycle_fail_without_disclosing_child(assembly_db):
    db, products, rows, other = assembly_db
    products[2].customer_id = other.id
    db.commit()
    with pytest.raises(HTTPException) as caught:
        component_context(db, products[0])
    assert "跨客户" in caught.value.detail
    products[2].customer_id = products[0].customer_id
    db.add(ProductBomComponent(parent_product_id=products[2].id,component_product_id=products[0].id,
            quantity_per_set=1,display_order=1,internal_component_code="cycle",is_die_cut=False))
    db.commit()
    with pytest.raises(HTTPException) as caught:
        component_context(db, products[0])
    assert "循环" in caught.value.detail


def test_nested_assembly_release_covers_children_once(assembly_db):
    db, products, _, _ = assembly_db
    root, inner, *_ = products
    geometry = {"width_mm":"0","height_mm":"0","panels":[],"cut":[],"score":[]}
    release = DrawingRelease(product_id=inner.id,customer_id=inner.customer_id,revision="R1",
                 external_number="inner-drawing",internal_number="inner-drawing",idempotency_key="inner-release",
                 design_version=1,product_version=1,template_key="assembly_v1",
                 manifest_json=json.dumps({"geometry":geometry,"fold_model":[],
                    "editor_state":{"assembly":{"placements":[{"frozen":"test"}]}}}),
                 pdf_reference="test-unused",pdf_sha256="0"*64)
    db.add(release);db.commit()
    context = component_context(db,root)
    whole = {"path":context["instances"][0]["path"],"instance_index":0,"child_release_id":release.id,
             "position_mm":[0,0,0],"rotation_deg":[0,0,0]}
    assert len(validate_component_placements(db,root,[whole],for_publish=True)) == 1
    with pytest.raises(HTTPException) as caught:
        validate_component_placements(db,root,[whole]+placements_for(context))
    assert "重复摆放" in caught.value.detail
