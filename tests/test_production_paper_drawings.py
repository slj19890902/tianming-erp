"""Owned paper previews preserve figure identity without changing business facts."""
from io import BytesIO
from datetime import date, datetime
import json

from fastapi.testclient import TestClient
from PIL import Image
import pytest
from sqlalchemy import select

from tests.test_p1_32a2_requisition_production_print import production_print_app, _login
from tests.test_p0_38_stock_replenishment_print import stock_replenishment_print_app
from app.models.order import OrderItem
from app.models.product import Product
from app.models.product_drawing import ProductDrawing
from app.models.product_bom import SalesOrderItemBomComponent, RequisitionItemBomSource
from app.models.requisition import Requisition, RequisitionItem
from app.models.stock_replenishment import StockReplenishmentOrderItem
from app.models.supplier_requisition_order import SupplierRequisitionOrder
from app.services.production_paper_drawings import order_paper_drawings, order_sources, stock_sources
from app.services.requisition_production_print import build_supplier_requisition_production_package, build_stock_replenishment_production_package
from app.services.stock_purchase_identity import capture


def image_bytes(color="white"):
    buffer = BytesIO()
    Image.new("RGB", (900, 600), color).save(buffer, "PNG")
    return buffer.getvalue()


def save_file(root, filename, content):
    root.mkdir(parents=True, exist_ok=True)
    (root / filename).write_bytes(content)
    return "private:" + filename


def pdf_bytes():
    import fitz
    with fitz.open() as document:
        document.new_page(width=900, height=700).insert_text((50, 50), "Synthetic paper drawing")
        return document.tobytes()


def test_all_reference_uploads_have_image_pdf_previews_and_no_business_write(production_print_app, tmp_path, monkeypatch):
    fixture = production_print_app
    root = tmp_path / "files"
    monkeypatch.setenv("ERP_FILE_STORAGE_DIR", str(root))
    with fixture["session_factory"]() as db:
        item = db.get(OrderItem, fixture["order_item_id"])
        item.drawing_file = None
        contents = [image_bytes(), pdf_bytes()]
        refs = [save_file(root, "engineering.png", contents[0]), save_file(root, "engineering.pdf", contents[1])]
        uploads = [ProductDrawing(product_id=item.product_id, image_path=ref, thumbnail_path=ref) for ref in refs]
        db.add_all(uploads)
        db.add(ProductDrawing(product_id=item.product_id, image_path="private:drawing_originals/art.png", thumbnail_path="private:art.png"))
        db.commit()
        item_id = item.id
        rows = order_paper_drawings(db, item)
        assert len(rows) == 2 and {row["kind"] for row in rows} == {"image", "pdf"}
        assert all(row["basis"] == "current_reference" and row["source_label"] == "参考图" for row in rows)
        assert "private:" not in json.dumps(rows)
        supplier = db.get(SupplierRequisitionOrder, fixture["supplier_order_id"])
        package = build_supplier_requisition_production_package(db, supplier)
        components = [c for card in package["cards"] for c in card["components"] if c["order_item_id"] == item_id]
        assert all(len(c["paper_drawings"]) == 2 for c in components)
        before = (item.quantity, item.subtotal, item.drawing_file, item.material_status)
    with TestClient(fixture["app"]) as client:
        _login(client, "p132a2-admin")
        for row in rows:
            preview = client.get(row["preview_url"])
            assert preview.status_code == 200, preview.text
            assert preview.headers["content-type"] == "image/webp"
            assert "no-store" in preview.headers["cache-control"]
            with Image.open(BytesIO(preview.content)) as image:
                assert max(image.size) <= 640 and image.width > image.height
            original = client.get(row["original_url"])
            assert original.status_code == 200
            assert original.content in contents
    with fixture["session_factory"]() as db:
        item = db.get(OrderItem, item_id)
        assert (item.quantity, item.subtotal, item.drawing_file, item.material_status) == before


def test_order_attachment_stays_exact_when_new_product_upload_arrives(production_print_app, tmp_path, monkeypatch):
    fixture = production_print_app
    root = tmp_path / "files"
    monkeypatch.setenv("ERP_FILE_STORAGE_DIR", str(root))
    with fixture["session_factory"]() as db:
        item = db.get(OrderItem, fixture["order_item_id"])
        item.drawing_file = save_file(root, "order.png", image_bytes())
        db.commit()
        before = order_paper_drawings(db, item)
        newer = save_file(root, "new.png", image_bytes("red"))
        db.add(ProductDrawing(product_id=item.product_id, image_path=newer, thumbnail_path=newer))
        db.get(Product, item.product_id).die_cut_path = newer
        db.commit()
        assert order_paper_drawings(db, item) == before
        assert before[0]["basis"] == "order_attachment"
    with TestClient(fixture["app"]) as client:
        _login(client, "p132a2-admin")
        assert client.get(before[0]["original_url"]).content == image_bytes()


def test_bom_child_never_uses_parent_order_attachment(production_print_app, tmp_path, monkeypatch):
    fixture = production_print_app
    root = tmp_path / "files"
    monkeypatch.setenv("ERP_FILE_STORAGE_DIR", str(root))
    with fixture["session_factory"]() as db:
        item = db.get(OrderItem, fixture["order_item_id"])
        child = Product(customer_id=item.order.customer_id, product_code="CHILD", customer_material_code="CHILD",
                        product_name="真实子件", box_category="normal")
        db.add(child); db.flush()
        ref = save_file(root, "child.png", image_bytes())
        component = SalesOrderItemBomComponent(sales_order_item_id=item.id, component_product_id=child.id,
            order_set_quantity=200, quantity_per_set=1, required_piece_quantity=200, display_order=1,
            internal_component_code="CHILD", is_die_cut=False, snapshot_die_cut_path=ref,
            spare_sheet_quantity=0, display_mode="internal_only", is_required=True,
            snapshot_component_product_code="CHILD", snapshot_component_product_name="真实子件",
            snapshot_component_box_category="normal")
        db.add(component); db.commit()
        frozen = order_paper_drawings(db, item, component)
        assert frozen[0]["product_id"] == child.id and frozen[0]["basis"] == "order_bom_snapshot"
        assert f"/bom-component/{component.id}/" in frozen[0]["preview_url"]
        with TestClient(fixture["app"]) as client:
            _login(client, "p132a2-admin")
            assert client.get(frozen[0]["preview_url"]).status_code == 404  # Not yet reported.
        requisition = Requisition(requisition_number="UAT-CHILD-DRAWING", requisition_date=date(2026, 10, 10))
        db.add(requisition); db.flush()
        row = RequisitionItem(requisition_id=requisition.id, order_item_id=item.id, requisition_qty=200,
                              cardboard_len=600, cardboard_width=400, product_name_snapshot="真实子件")
        db.add(row); db.flush()
        db.add(RequisitionItemBomSource(requisition_item_id=row.id, sales_order_item_bom_component_id=component.id,
            order_set_quantity=200, quantity_per_set=1, required_piece_quantity=200, spare_sheet_quantity=0,
            calculated_purchase_quantity=200))
        db.commit()
        with TestClient(fixture["app"]) as client:
            _login(client, "p132a2-admin")
            assert client.get(frozen[0]["preview_url"]).status_code == 200
        child.die_cut_path = save_file(root, "new-child.png", image_bytes("red"))
        db.commit()
        assert order_paper_drawings(db, item, component) == frozen
        component.snapshot_die_cut_path = None
        db.commit()
        references = order_sources(db, item, component)
        assert len(references) == 1 and references[0].product_id == child.id
        assert references[0].basis == "current_reference"
        child.die_cut_path = None
        db.commit()
        assert order_sources(db, item, component) == []  # Parent has an order drawing, but child does not.


def test_stock_purchase_pointer_does_not_follow_master_and_missing_evidence_is_not_guessed(stock_replenishment_print_app, tmp_path, monkeypatch):
    fixture = stock_replenishment_print_app
    root = tmp_path / "files"
    monkeypatch.setenv("ERP_FILE_STORAGE_DIR", str(root))
    with fixture["session_factory"]() as db:
        item = db.get(StockReplenishmentOrderItem, fixture["semi_item_id"])
        product = item.reference_product
        product.die_cut_path = save_file(root, "stock.pdf", pdf_bytes())
        item.production_snapshot_json = json.dumps(capture(product, item))
        db.commit()
        before = build_stock_replenishment_production_package(db, item.order, selected_item_ids={item.id})
        drawing = before["cards"][0]["components"][0]["paper_drawings"][0]
        assert drawing["basis"] == "purchase_snapshot"
        product.die_cut_path = save_file(root, "new-stock.png", image_bytes("red"))
        db.commit()
        after = build_stock_replenishment_production_package(db, item.order, selected_item_ids={item.id})
        assert after["cards"][0]["components"][0]["paper_drawings"] == [drawing]
        assert after["cards"][0]["planned_finished_quantity"] == before["cards"][0]["planned_finished_quantity"]
        with TestClient(fixture["app"]) as client:
            _login(client, "p132a2-admin")
            assert client.get(drawing["preview_url"]).status_code == 200
        item.production_snapshot_json = "{}"
        db.commit()
        assert stock_sources(db, item) == []
        item.production_snapshot_json = json.dumps(capture(product, item))
        db.commit()
    with TestClient(fixture["app"]) as client:
        _login(client, "p132a2-admin")
        assert client.get(drawing["preview_url"]).status_code == 404  # Exact pointer changed; old key cannot select new file.


@pytest.mark.parametrize("reference,content,expected", [
    ("private:../escape.pdf", None, 404), ("private:missing.png", None, 404),
    ("private:bad.png", b"<html>not an image</html>", 422),
    ("private:bad.pdf", b"%PDF-broken", 422),
])
def test_bad_frozen_source_has_explicit_failure(production_print_app, tmp_path, monkeypatch, reference, content, expected):
    fixture = production_print_app
    root = tmp_path / "files"
    monkeypatch.setenv("ERP_FILE_STORAGE_DIR", str(root))
    with fixture["session_factory"]() as db:
        item = db.get(OrderItem, fixture["order_item_id"])
        item.drawing_file = reference
        if content is not None:
            save_file(root, reference.removeprefix("private:"), content)
        db.commit()
        drawing = order_paper_drawings(db, item)[0]
    with TestClient(fixture["app"]) as client:
        _login(client, "p132a2-admin")
        response = client.get(drawing["preview_url"])
        assert response.status_code == expected and "no-store" in response.headers["cache-control"]


def test_preview_checks_permission_customer_and_attachment_owner_before_io(production_print_app, tmp_path, monkeypatch):
    fixture = production_print_app
    root = tmp_path / "files"
    monkeypatch.setenv("ERP_FILE_STORAGE_DIR", str(root))
    with fixture["session_factory"]() as db:
        item = db.get(OrderItem, fixture["order_item_id"])
        item.drawing_file = save_file(root, "order.png", image_bytes())
        db.commit()
        drawing = order_paper_drawings(db, item)[0]
    with TestClient(fixture["app"]) as client:
        assert client.get(drawing["preview_url"]).status_code == 401
        _login(client, "p132a2-sales")
        assert client.get(drawing["preview_url"]).status_code == 403
        monkeypatch.setattr("app.services.mobile_product_drawings.resolve_stored_reference", lambda *_: pytest.fail("Denied source opened a file"))
        assert client.get(drawing["preview_url"]).status_code == 403
        client.cookies.clear(); _login(client, "p132a2-admin")
        wrong = drawing["preview_url"].replace(drawing["key"], "attachment-999999")
        assert client.get(wrong).status_code == 404


def test_managed_figure_contract_uses_only_frozen_structure():
    from app.services.production_paper_drawings import managed_public
    drawing = dict(release_id=71, product_id=8, number="DW71", revision="A",
                   svg_urls={"structure": "data:image/svg+xml;base64,PHN2Zy8+", "print": "different-artwork"},
                   pdf_url="/api/master/products/8/managed-drawing/releases/71/file")
    value = managed_public(drawing, "CHILD")
    assert value["preview_url"] == drawing["svg_urls"]["structure"]
    assert value["basis"] == "task_release" and value["source_label"] == "任务图"


@pytest.mark.parametrize("reference", ["private:drawing_originals/art.png",
    "private:DRAWING_ORIGINALS/art.png", "private:drawings/../drawing_originals/art.png"])
def test_frozen_pointer_cannot_relabel_print_artwork(production_print_app, monkeypatch, reference):
    with production_print_app["session_factory"]() as db:
        item = db.get(OrderItem, production_print_app["order_item_id"])
        item.drawing_file = reference
        db.commit()
        monkeypatch.setattr("app.services.mobile_product_drawings.resolve_stored_reference",
                            lambda *_: pytest.fail("Artwork exclusion opened a file"))
        assert order_paper_drawings(db, item) == []


@pytest.mark.parametrize("owner_type", ["order-item", "bom-component", "stock-item"])
def test_incoming_only_permission_requires_exact_posted_source_for_preview_and_original(stock_replenishment_print_app, tmp_path, monkeypatch, owner_type):
    from app.models.user import User
    from app.models.access_control import UserPermissionOverride, UserCustomerScope
    from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
    fixture = stock_replenishment_print_app
    root = tmp_path / "files"
    monkeypatch.setenv("ERP_FILE_STORAGE_DIR", str(root))
    with fixture["session_factory"]() as db:
        item = db.get(OrderItem, fixture["order_item_id"])
        user = db.scalar(select(User).where(User.username == "p132a2-sales"))
        admin = db.scalar(select(User).where(User.role == "admin"))
        db.scalar(select(UserPermissionOverride).where(UserPermissionOverride.user_id == user.id,
            UserPermissionOverride.permission_code == "requisition.view")).is_allowed = False
        db.add(UserPermissionOverride(user_id=user.id, permission_code="incoming.view", is_allowed=True))
        scope = db.scalar(select(UserCustomerScope).where(UserCustomerScope.user_id == user.id))
        other_customer_id = scope.customer_id
        scope.customer_id = item.order.customer_id
        ref = save_file(root, "incoming.png", image_bytes())
        kwargs = dict(order_id=item.order_id, order_item_id=item.id)
        if owner_type == "stock-item":
            stock = db.get(StockReplenishmentOrderItem, fixture["semi_item_id"])
            stock.reference_product.die_cut_path = ref
            stock.production_snapshot_json = json.dumps(capture(stock.reference_product, stock))
            db.commit()
            drawing = stock_sources(db, stock)[0].public("stock-item", stock.id)
            kwargs = dict(stock_replenishment_item_id=stock.id)
        elif owner_type == "bom-component":
            child = Product(customer_id=item.order.customer_id, product_code="RECEIVED-CHILD",
                customer_material_code="RECEIVED-CHILD", product_name="实收子件", box_category="normal")
            db.add(child); db.flush()
            component = SalesOrderItemBomComponent(sales_order_item_id=item.id, component_product_id=child.id,
                order_set_quantity=200, quantity_per_set=1, required_piece_quantity=200, display_order=1,
                internal_component_code="RECEIVED-CHILD", is_die_cut=False, snapshot_die_cut_path=ref,
                spare_sheet_quantity=0, display_mode="internal_only", is_required=True,
                snapshot_component_product_code="RECEIVED-CHILD", snapshot_component_product_name="实收子件",
                snapshot_component_box_category="normal")
            requisition = Requisition(requisition_number="UAT-INCOMING-CHILD", requisition_date=date(2026, 10, 10))
            db.add_all([component, requisition]); db.flush()
            source = RequisitionItem(requisition_id=requisition.id, order_item_id=item.id, requisition_qty=200,
                cardboard_len=600, cardboard_width=400, product_name_snapshot="实收子件")
            db.add(source); db.flush()
            db.add(RequisitionItemBomSource(requisition_item_id=source.id, sales_order_item_bom_component_id=component.id,
                order_set_quantity=200, quantity_per_set=1, required_piece_quantity=200,
                spare_sheet_quantity=0, calculated_purchase_quantity=200))
            db.commit()
            drawing = order_paper_drawings(db, item, component)[0]
            kwargs.update(requisition_id=requisition.id, requisition_item_id=source.id)
        else:
            item.drawing_file = ref
            db.commit()
            drawing = order_paper_drawings(db, item)[0]
        with TestClient(fixture["app"]) as client:
            _login(client, "p132a2-sales")
            assert client.get(drawing["preview_url"]).status_code == 403
            assert client.get(drawing["original_url"]).status_code == 403
        receipt = IncomingReceipt(receipt_number="UAT-PAPER-RECEIVED", received_at=datetime(2026, 10, 10),
            received_by=admin.id, idempotency_key="uat-paper-received")
        db.add(receipt); db.flush()
        fact = IncomingReceiptItem(receipt_id=receipt.id, planned_quantity=200, received_quantity=200,
            cumulative_received_quantity=200, variance_quantity=0, variance_type="matched",
            resolution_status="not_required", status="posted", **kwargs)
        db.add(fact); db.commit()
        with TestClient(fixture["app"]) as client:
            _login(client, "p132a2-sales")
            assert client.get(drawing["preview_url"]).status_code == 200
            assert client.get(drawing["original_url"]).content == image_bytes()
            fact.status = "reversed"; db.commit()
            assert client.get(drawing["preview_url"]).status_code == 403
            assert client.get(drawing["original_url"]).status_code == 403
            fact.status = "posted"; scope.customer_id = other_customer_id; db.commit()
            assert client.get(drawing["preview_url"]).status_code == 403
            assert client.get(drawing["original_url"]).status_code == 403
