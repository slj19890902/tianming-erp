"""Disposable station API checks for managed drawing/mold identity."""
import hashlib
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models.drawing_design import DrawingRelease, ProductionTaskDrawing
from app.models.mold_tool import MoldTool
from app.models.product import Product
from app.models.production import ProductionTask
from app.services.secure_uploads import ValidatedUpload, store_private_upload
from test_p1_21d_mobile_production_materials import _login, mobile_production_app
from test_p1_21f1_mobile_incoming_dimension_search import mobile_incoming_search_app


@pytest.mark.parametrize("published_with_mold", [True, False])
def test_mobile_uses_release_mold_and_current_location_without_changing_legacy(
    mobile_production_app, tmp_path, monkeypatch, published_with_mold
):
    app, factory, ids = mobile_production_app
    monkeypatch.setenv("ERP_FILE_STORAGE_DIR", str(tmp_path / "drawing-files"))
    content = b"%PDF-1.4\n% isolated managed drawing response fixture\n%%EOF\n"
    reference = store_private_upload(
        ValidatedUpload(content, "isolated.pdf", ".pdf", "application/pdf", len(content),
                        hashlib.sha256(content).hexdigest()),
        category="drawing-v2",
    ).reference
    with factory() as db:
        product = db.scalar(select(Product).where(Product.product_code == "MOBILE-PROD-01"))
        original_mold = db.get(MoldTool, product.mold_tool_id)
        task = db.scalar(select(ProductionTask).where(ProductionTask.order_item_id == ids["visible_item"]))
        release = DrawingRelease(
            product_id=product.id, customer_id=product.customer_id, revision="R1",
            external_number="UAT-MOBILE-1", internal_number="TM-DWG-20260922-001",
            idempotency_key="isolated-mobile-release", design_version=1, product_version=1,
            template_key="custom_21301634_v1", pdf_reference=reference,
            pdf_sha256=hashlib.sha256(content).hexdigest(),
            mold_tool_id=original_mold.id if published_with_mold else None,
            manifest_json=json.dumps({"mold_snapshot": {
                "code": original_mold.mold_code, "name": original_mold.mold_name,
            } if published_with_mold else None}),
        )
        db.add(release)
        db.flush()
        db.add(ProductionTaskDrawing(task_id=task.id, release_id=release.id))
        replacement = MoldTool(mold_code="NEW-MOLD", mold_name="后换模具", rack_location="M1-R09")
        db.add(replacement)
        db.flush()
        product.mold_tool_id = replacement.id
        original_mold.rack_location = "M1-R06"
        original_mold.is_active = False
        db.commit()
        task_id, original_mold_id = task.id, original_mold.id

    with TestClient(app) as client:
        _login(client, "mobile-workshop")
        response = client.get("/api/mobile/erp/production/tasks", params={"station": "die_cut"})
        assert response.status_code == 200, response.text
        rows = response.json()["items"]
        managed = next(row for row in rows if row["task_id"] == task_id)
        legacy = next(row for row in rows if row["task_id"] != task_id)
        assert managed["drawing_kind"] == "pdf"
        if published_with_mold:
            assert managed["mold_name"] == "匿名内盒模具"
            assert managed["mold_location"] == "M1-R06"
            assert managed["mold_is_active"] is False
            assert "禁止直接生产" in managed["mold_warning"]
            assert managed["mold_map_url"] == f"/mobile/mold-lookup?mold_id={original_mold_id}&readonly=1"
        else:
            assert managed["mold_name"] is None
            assert managed["mold_location"] is None
            assert managed["mold_map_url"] is None
        assert legacy["mold_name"] == "后换模具"
        assert legacy["mold_location"] == "M1-R09"
        drawing = client.get(managed["drawing_path"])
        assert drawing.status_code == 200
        assert drawing.content == content
        assert drawing.headers["content-type"] == "application/pdf"
        recent = client.get("/api/mobile/erp/production/recent")
        assert recent.status_code == 200, recent.text
        recent_task = next(task for row in recent.json()["items"] for task in row["production_tasks"]
                           if task["task_id"] == task_id)
        assert recent_task["drawing_path"] == managed["drawing_path"]
        assert recent_task["mold_location"] == managed["mold_location"]
        lookup = client.get("/api/mobile/erp/production/pending/lookup", params={"q": "MOBILE-PROD-01"})
        assert lookup.status_code == 200, lookup.text
        assert next(row for row in lookup.json()["items"] if row["task_id"] == task_id)["drawing_path"] == managed["drawing_path"]
        assert next(row for row in lookup.json()["items"] if row["task_id"] != task_id)["drawing_path"] is None

        # Keep the original pending-state gate; a completed task is not exposed.
        with factory() as db:
            db.get(ProductionTask, task_id).status = "completed"
            db.commit()
        assert client.get(managed["drawing_path"]).status_code == 404
        completed_recent = client.get("/api/mobile/erp/production/recent")
        assert completed_recent.status_code == 200, completed_recent.text
        assert next(task for row in completed_recent.json()["items"] for task in row["production_tasks"]
                    if task["task_id"] == task_id)["drawing_path"] is None


def test_incoming_detail_managed_drawing_is_task_scoped_and_waiting_has_no_fallback(mobile_incoming_search_app):
    app, factory, ids = mobile_incoming_search_app
    with factory() as db:
        task = db.get(ProductionTask, ids["first_task"])
        from app.models.order import OrderItem
        product = db.get(Product, db.get(OrderItem, task.order_item_id).product_id)
        release = DrawingRelease(
            product_id=product.id, customer_id=product.customer_id, revision="R1",
            external_number="UAT-INCOMING-1", internal_number="TM-DWG-20260922-001",
            idempotency_key="isolated-incoming-release", design_version=1, product_version=1,
            template_key="liner_v1", pdf_reference="private:isolated-unopened.pdf",
            pdf_sha256="0" * 64, manifest_json="{}",
        )
        db.add(release)
        db.flush()
        db.add(ProductionTaskDrawing(task_id=task.id, release_id=release.id))
        planned_quantity = task.planned_quantity
        task.status = "waiting_material"
        task.planned_quantity = 0
        db.commit()
    with TestClient(app) as client:
        response = client.post("/api/auth/login", json={"username": "p1-21f1-worker", "password": "123456"})
        assert response.status_code == 200
        detail = client.get(f"/api/mobile/erp/incoming/{ids['first_item']}/production-detail")
        assert detail.status_code == 200, detail.text
        assert detail.json()["production_tasks"][0]["drawing_path"] is None
        with factory() as db:
            task = db.get(ProductionTask, ids["first_task"])
            task.status = "pending"
            task.planned_quantity = planned_quantity
            db.commit()
        detail = client.get(f"/api/mobile/erp/incoming/{ids['first_item']}/production-detail")
        assert detail.status_code == 200, detail.text
        assert detail.json()["production_tasks"][0]["drawing_path"] == f"/api/mobile/erp/production/tasks/{ids['first_task']}/drawing"
        other = client.get(f"/api/mobile/erp/incoming/{ids['second_item']}/production-detail")
        assert other.status_code == 200, other.text
        assert other.json()["production_tasks"][0]["drawing_path"] == other.json()["incoming_item"]["drawing_path"]
