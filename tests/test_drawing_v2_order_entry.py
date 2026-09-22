"""Real authenticated order entry freezes each product/component's V2 release.

Existing fixtures override only get_db with pytest-created SQLite databases.
Authentication, permission/customer checks, order creation, and binding services
are real. Products/BOM fixture data are synthetic; files use explicit tmp_path.
"""
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.deps import get_db
from app.api.drawing_design import router as drawing_router
from app.models.drawing_design import ProductionTaskDrawing
from app.models.order import OrderItem
from app.models.product import Product
from app.models.product_bom import SalesOrderItemBomComponent
from app.models.production import ProductionTask
from app.services.drawing_binding import bound_task_release
from app.services.secure_uploads import resolve_stored_reference
from test_phase5_orders import _login as order_login, order_api_app
from test_n039_composite_bom_requisition import _login as composite_login, composite_requisition_app


CASES = {
    "liner_v1": {"style": "衬板", "length": "620.25", "width": "470.5", "height": None, "parameters": {}},
    "slotted_v1": {"style": "A1/0201", "length": "300.25", "width": "200.5", "height": "400.75",
        "parameters": {"panel_1_mm": "300.25", "panel_2_mm": "200.5", "panel_3_mm": "300.25",
            "panel_4_mm": "200.5", "body_height_mm": "400.75", "top_flap_mm": "100.25",
            "bottom_flap_mm": "100.25", "glue_flap_mm": "30", "slot_width_mm": "5"}},
    "custom_21301634_v1": {"style": "模切内盒", "length": "620", "width": "470", "height": "26",
        "parameters": {"top_cover_mm": "235", "bottom_cover_mm": "235", "top_fold_mm": "28",
            "bottom_fold_mm": "28", "left_fold_mm": "26", "right_fold_mm": "26",
            "left_wing_mm": "35.5", "right_wing_mm": "40"}},
}


def configure_product(product, template):
    case = CASES[template]
    product.box_style = case["style"]
    product.length_mm = Decimal(case["length"])
    product.width_mm = Decimal(case["width"])
    product.height_mm = Decimal(case["height"]) if case["height"] is not None else None
    product.splice_mode = "single"
    product.pieces_per_box = 1
    product.flap_mm = 30
    product.flute_type = "AB"  # The ordinary-order fixture supplies five-layer material.


def publish_http(client, product_id, product_version, template):
    base = f"/api/master/products/{product_id}/managed-drawing"
    saved = client.put(base, json={"expected_product_version": product_version,
        "idempotency_key": f"order-entry-save-{product_id}", "template_key": template,
        "parameters": CASES[template]["parameters"], "thickness_mm": "7", "thickness_source": "隔离合成测试"})
    assert saved.status_code == 200, saved.text
    assert saved.json()["geometry_ready"]
    released = client.post(base + "/releases", json={"expected_product_version": product_version,
        "expected_design_version": saved.json()["draft"]["version"],
        "idempotency_key": f"order-entry-publish-{product_id}"})
    assert released.status_code == 200, released.text
    return released.json()["id"]


def order_payload(product_id, *, quantity=10):
    return {"customer_id": 1, "customer_po": "DRAWING-ORDER-ENTRY-UAT", "order_date": "2026-09-22",
            "delivery_date": "2026-09-29", "items": [{"product_id": product_id, "quantity": quantity,
                                                        "unit_price": "3.60"}]}


@pytest.mark.parametrize("template", list(CASES))
def test_three_templates_bind_through_real_new_order_http(order_api_app, tmp_path, monkeypatch, template):
    app, session_factory = order_api_app
    assert set(app.dependency_overrides) == {get_db}
    storage = tmp_path / "order-entry-files"
    monkeypatch.setenv("ERP_FILE_STORAGE_DIR", str(storage))
    monkeypatch.setenv("ERP_DRAWING_V2_ENABLED", "1")
    with session_factory() as db:
        product = db.get(Product, 1)
        configure_product(product, template)
        db.commit()
        product_version = product.version
    with TestClient(app) as client:
        # Prove no authentication override was introduced for the new entry.
        anonymous = client.post("/api/orders", json=order_payload(1))
        assert anonymous.status_code == 401, anonymous.text
        order_login(client)
        release_id = publish_http(client, 1, product_version, template)
        created = client.post("/api/orders", json=order_payload(1))
        assert created.status_code == 201, created.text
        item_id = created.json()["items"][0]["id"]
    with session_factory() as db:
        item = db.get(OrderItem, item_id)
        tasks = db.scalars(select(ProductionTask).where(ProductionTask.order_item_id == item_id)).all()
        assert len(tasks) == 1 and tasks[0].sales_order_item_bom_component_id is None
        binding = db.get(ProductionTaskDrawing, tasks[0].id)
        assert binding is not None and binding.release_id == release_id
        release = bound_task_release(db, tasks[0].id)
        assert (release.product_id, release.product_version, release.template_key) == (1, product_version, template)
        assert item.quantity == 10 and tasks[0].ordered_quantity_snapshot == 10
        assert tasks[0].status == "waiting_material"
        assert item.snapshot_spec == "×".join(value for value in
            (CASES[template]["length"], CASES[template]["width"], CASES[template]["height"]) if value) + "mm"
        path = resolve_stored_reference(release.pdf_reference)
        assert path.is_file() and path.is_relative_to(storage)
        assert path.read_bytes().startswith(b"%PDF")


def test_composite_order_http_binds_each_component_release_not_the_first(composite_requisition_app, tmp_path, monkeypatch):
    app, session_factory = composite_requisition_app
    assert set(app.dependency_overrides) == {get_db}
    app.include_router(drawing_router, prefix="/api/master/products")
    storage = tmp_path / "component-entry-files"
    monkeypatch.setenv("ERP_FILE_STORAGE_DIR", str(storage))
    monkeypatch.setenv("ERP_DRAWING_V2_ENABLED", "1")
    with session_factory() as db:
        parent = db.scalar(select(Product).where(Product.product_code == "KIT-001"))
        components = [db.scalar(select(Product).where(Product.product_code == code)) for code in ("COMP-A", "COMP-B")]
        templates = ("custom_21301634_v1", "liner_v1")
        for product, template in zip(components, templates):
            configure_product(product, template)
        db.commit()
        parent_id = parent.id
        product_sources = [(p.id, p.version, template) for p, template in zip(components, templates)]
    with TestClient(app) as client:
        composite_login(client)
        published = {product_id: publish_http(client, product_id, version, template)
                     for product_id, version, template in product_sources}
        assert len(set(published.values())) == 2
        created = client.post("/api/orders", json=order_payload(parent_id))
        assert created.status_code == 201, created.text
        item_id = created.json()["items"][0]["id"]
    with session_factory() as db:
        snapshots = db.scalars(select(SalesOrderItemBomComponent).where(
            SalesOrderItemBomComponent.sales_order_item_id == item_id)).all()
        tasks = db.scalars(select(ProductionTask).where(ProductionTask.order_item_id == item_id)).all()
        assert len(snapshots) == len(tasks) == 2
        by_snapshot = {task.sales_order_item_bom_component_id: task for task in tasks}
        assert None not in by_snapshot
        actual_releases = set()
        for snapshot in snapshots:
            task = by_snapshot[snapshot.id]
            release = bound_task_release(db, task.id)
            assert release is not None and release.id == published[snapshot.component_product_id]
            assert release.product_version == snapshot.component_product_version
            assert release.product_id == snapshot.component_product_id and release.customer_id == 1
            actual_releases.add(release.id)
            assert snapshot.required_piece_quantity == 10 * snapshot.quantity_per_set
            assert task.status == "waiting_material"
            path = resolve_stored_reference(release.pdf_reference)
            assert path.is_file() and path.is_relative_to(storage)
        assert actual_releases == set(published.values())
        assert db.get(OrderItem, item_id).quantity == 10
