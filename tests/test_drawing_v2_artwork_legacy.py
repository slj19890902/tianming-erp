"""Uploading a Logo must not take over unadopted legacy production drawings."""
import asyncio
import hashlib
from io import BytesIO

import pytest
from fastapi import UploadFile
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import delete, select
from starlette.datastructures import Headers

from app.api.products import _create_drawing_version, get_product
from app.models.order import OrderItem
from app.models.product import Product
from app.models.product_drawing import ProductDrawing
from app.models.production import ProductionTask
from app.models.supplier_requisition_order import SupplierRequisitionOrder
from app.models.user import User
from app.services.product_drawings import default_product_drawing, engineering_drawing_condition
from app.services.secure_uploads import ValidatedUpload, resolve_stored_reference, store_private_upload
from test_p1_21d_mobile_production_materials import _login, mobile_production_app
from test_p1_32a2_requisition_production_print import production_print_app


def add_legacy(db, product_id):
    content = b"%PDF-1.4\n% old isolated engineering drawing\n%%EOF\n"
    stored = store_private_upload(ValidatedUpload(content, "old-structure.pdf", ".pdf", "application/pdf",
                                   len(content), hashlib.sha256(content).hexdigest()), category="drawings")
    row = ProductDrawing(product_id=product_id, image_path=stored.reference, thumbnail_path=stored.reference)
    db.add(row)
    db.commit()
    return row, content


def logo_bytes():
    image = Image.new("RGBA", (100, 50), (0, 0, 0, 0))
    image.paste("red", (20, 10, 80, 40))
    buffer = BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def upload_logo(db, product_id, user):
    file = UploadFile(file=BytesIO(logo_bytes()), filename="logo.png", headers=Headers({"content-type": "image/png"}))
    return asyncio.run(_create_drawing_version(product_id, file, db, user, preserve_original=True))


def test_unpublished_logo_keeps_legacy_paper_order_and_incoming_sources(production_print_app, tmp_path, monkeypatch):
    from app.api.incoming import _rows
    from app.api.orders import get_order_detail
    from app.services.requisition_production_print import build_supplier_requisition_production_package
    monkeypatch.setenv("ERP_FILE_STORAGE_DIR", str(tmp_path / "private-files"))
    with production_print_app["session_factory"]() as db:
        product_id = production_print_app["product_id"]
        user = db.scalar(select(User).where(User.role == "admin"))
        for item in db.scalars(select(OrderItem).where(OrderItem.product_id == product_id)):
            item.drawing_file = None
        db.commit()
        old, _ = add_legacy(db, product_id)
        supplier = db.get(SupplierRequisitionOrder, production_print_app["supplier_order_id"])
        before = build_supplier_requisition_production_package(db, supplier)
        logo = upload_logo(db, product_id, user)
        db.expire_all()
        after = build_supplier_requisition_production_package(db, supplier)
        old_url = f"/api/master/products/drawings/{old.id}/content/original.pdf"
        paper_url = f"/api/master/products/drawings/{old.id}/content/thumbnail.pdf"
        relevant_before = [c["structure_reference"] for c in before["cards"] if c["structure_reference"] and c["structure_reference"]["url"] == paper_url]
        relevant_after = [c["structure_reference"] for c in after["cards"] if c["structure_reference"] and c["structure_reference"]["url"] == paper_url]
        assert relevant_before and relevant_after == relevant_before
        assert not any(c["managed_drawings"] for c in after["cards"])
        detail = get_order_detail(production_print_app["sales_order_id"], db, user)
        matching = [item for item in detail["items"] if item["product_id"] == product_id]
        assert matching and all(item["product_drawing_file"] == old_url for item in matching)
        incoming = [row for row in _rows(db, user=user) if row.get("product_id") == product_id]
        assert incoming and all(row["product_drawing_path"] == old_url for row in incoming)
        gallery = get_product(product_id, db, user)["drawings"]
        assert {row["id"]: row["purpose"] for row in gallery} == {old.id: "engineering", logo.id: "print_artwork"}
        assert resolve_stored_reference(logo.image_path).read_bytes() == logo_bytes()


@pytest.mark.parametrize("has_legacy", [False, True])
def test_mobile_task_and_product_never_use_unpublished_logo(mobile_production_app, tmp_path, monkeypatch, has_legacy):
    from app.api.products import router as products_router
    monkeypatch.setenv("ERP_FILE_STORAGE_DIR", str(tmp_path / "private-files"))
    app, factory, ids = mobile_production_app
    app.include_router(products_router, prefix="/api/master/products")
    with factory() as db:
        item = db.get(OrderItem, ids["visible_item"])
        item.drawing_file = None
        product_id = item.product_id
        task_id = db.scalar(select(ProductionTask.id).where(ProductionTask.order_item_id == item.id))
        db.commit()
        old, old_content = add_legacy(db, product_id) if has_legacy else (None, None)
    with TestClient(app) as client:
        _login(client, "mobile-sales")
        response = client.post(f"/api/master/products/{product_id}/managed-drawing/assets",
                               files={"file": ("logo.png", logo_bytes(), "image/png")})
        assert response.status_code == 201, response.text
        assert response.json()["purpose"] == "print_artwork"
        # Authorized original access and gallery remain available.
        assert client.get(response.json()["image_path"]).content == logo_bytes()
        gallery = client.get(f"/api/master/products/{product_id}").json()["drawings"]
        assert any(row["purpose"] == "print_artwork" for row in gallery)
        _login(client, "mobile-workshop")
        rows = client.get("/api/mobile/erp/production/tasks", params={"station": "die_cut"}).json()["items"]
        task = next(row for row in rows if row["task_id"] == task_id)
        overview = client.get(f"/api/mobile/erp/products/{product_id}/production-overview")
        assert overview.status_code == 200, overview.text
        assert overview.json()["product"]["drawing_available"] == has_legacy
        if has_legacy:
            assert task["drawing_kind"] == "pdf"
            assert client.get(task["drawing_path"]).content == old_content
            assert client.get(f"/api/mobile/erp/products/{product_id}/drawing").content == old_content
        else:
            assert task["drawing_path"] is None
            assert client.get(f"/api/mobile/erp/products/{product_id}/drawing").status_code == 404


def test_engineering_replacement_preserves_logo_and_exact_category(production_print_app, tmp_path, monkeypatch):
    monkeypatch.setenv("ERP_FILE_STORAGE_DIR", str(tmp_path / "private-files"))
    with production_print_app["session_factory"]() as db:
        product_id = production_print_app["product_id"]
        user = db.scalar(select(User).where(User.role == "admin"))
        old, _ = add_legacy(db, product_id)
        logo = upload_logo(db, product_id, user)
        lookalike = ProductDrawing(product_id=product_id, image_path="private:drawingXoriginals/legacy.pdf", thumbnail_path="legacy.pdf")
        db.add(lookalike)
        db.commit()
        # SQL wildcard escaping must agree with exact Python category selection.
        records = db.scalars(select(ProductDrawing).where(ProductDrawing.product_id == product_id,
                             engineering_drawing_condition()).order_by(ProductDrawing.id.desc())).all()
        assert [row.id for row in records] == [lookalike.id, old.id]
        product = db.get(Product, product_id)
        assert default_product_drawing(product.drawings).id == lookalike.id
        db.execute(delete(ProductDrawing).where(ProductDrawing.product_id == product_id,
                                               engineering_drawing_condition()))
        db.commit()
        assert db.get(ProductDrawing, logo.id) is not None
        assert resolve_stored_reference(logo.image_path).read_bytes() == logo_bytes()
