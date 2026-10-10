from __future__ import annotations

from io import BytesIO
from datetime import datetime
import json

import fitz
from PIL import Image
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from app.models.access_control import UserPermissionOverride
from app.models.drawing_design import DrawingRelease
from app.models.product import Product
from app.models.product_drawing import ProductDrawing
from app.models.user import User
from app.services import mobile_product_drawings as drawings
from tests.test_p1_76_mobile_portal import mobile_portal_app
from tests.test_p1_21b_mobile_admin_product_search import mobile_erp_app, _login


@pytest.fixture
def drawing_app(mobile_portal_app, tmp_path, monkeypatch):
    from app.api.mobile_stock_use import router
    app, ids, factory = mobile_portal_app
    app.include_router(router, prefix="/api/mobile/erp")
    root = tmp_path / "attachments"
    root.mkdir()
    monkeypatch.setenv("ERP_FILE_STORAGE_DIR", str(root))
    monkeypatch.setenv("ERP_LEGACY_UPLOAD_DIR", str(root / "legacy"))
    Image.new("RGB", (1200, 900), "white").save(root / "engineering.png")
    Image.new("RGB", (240, 180), "red").save(root / "thumb.webp")
    document = fitz.open()
    document.new_page(width=1200, height=900).insert_text((50, 50), "Synthetic engineering drawing")
    document.new_page().insert_text((50, 50), "Page two is not a thumbnail")
    document.save(root / "engineering.pdf")
    document.close()
    with factory() as db:
        from app.models.warehouse_inventory import InventoryLot
        detail = db.get(InventoryLot, ids["finished_lot"]).finished_detail
        detail.length_mm, detail.width_mm, detail.height_mm = 420, 310, 260
        attachment = ProductDrawing(product_id=ids["product"], image_path="private:engineering.png",
                                    thumbnail_path="private:thumb.webp")
        pdf = ProductDrawing(product_id=ids["product_two"], image_path="private:engineering.pdf",
                             thumbnail_path="private:engineering.pdf")
        artwork = ProductDrawing(product_id=ids["product"], image_path="private:drawing_originals/secret.png",
                                 thumbnail_path="private:thumb.webp")
        db.add_all([attachment, pdf, artwork])
        db.commit()
        ids.update(attachment=attachment.id, pdf=pdf.id, artwork=artwork.id)
    return app, ids, factory, root


def urls(ids, *, pdf=False):
    product = ids["product_two"] if pdf else ids["product"]
    attachment = ids["pdf"] if pdf else ids["attachment"]
    return f"/api/mobile/erp/products/{product}/drawings/attachment-{attachment}"


def reader(factory, permission):
    with factory() as db:
        user = db.scalar(select(User).where(User.username == "mobile-picker"))
        for code in drawings.READ_PERMISSIONS:
            db.add(UserPermissionOverride(user_id=user.id, permission_code=code, is_allowed=code == permission))
        db.commit()


def test_search_is_lazy_and_image_pdf_preview_are_small_read_only(drawing_app, monkeypatch):
    app, ids, factory, root = drawing_app
    def forbidden_io(*args, **kwargs):
        raise AssertionError("Search opened an attachment")
    with TestClient(app) as client:
        _login(client, "mobile-admin")
        with monkeypatch.context() as patch:
            patch.setattr(drawings, "resolve_stored_reference", forbidden_io)
            patch.setattr(drawings, "drawing_preview", forbidden_io)
            response = client.get("/api/mobile/erp/products?q=MOBILE-BOX&include_zero=true")
            assert response.status_code == 200, response.text
            payload = response.json()
            assert len(payload["items"]) == 2
            for item in payload["items"]:
                assert item["drawings"]["status"] == "available"
                assert len(item["drawings"]["items"]) == 1
            assert "private:" not in json.dumps(payload)
            assert "secret" not in json.dumps(payload)
        files_before = {path.name: path.read_bytes() for path in root.iterdir()}
        for pdf in (False, True):
            response = client.get(urls(ids, pdf=pdf) + "/preview")
            assert response.status_code == 200, response.text
            assert response.headers["content-type"] == "image/webp"
            assert "no-store" in response.headers["cache-control"]
            with Image.open(BytesIO(response.content)) as image:
                assert max(image.size) <= 640
                assert image.width > image.height  # PDF page one is landscape.
            original = client.get(urls(ids, pdf=pdf) + "/original")
            assert original.status_code == 200
            assert original.content == (root / ("engineering.pdf" if pdf else "engineering.png")).read_bytes()
        assert files_before == {path.name: path.read_bytes() for path in root.iterdir()}
    with factory() as db:
        assert db.get(Product, ids["product"]).version == 1


@pytest.mark.parametrize("permission", drawings.READ_PERMISSIONS)
def test_existing_mobile_reader_permissions_match_metadata_and_content(drawing_app, permission):
    app, ids, factory, _ = drawing_app
    reader(factory, permission)
    with TestClient(app) as client:
        _login(client, "mobile-picker")
        response = client.get(urls(ids) + "/preview")
        assert response.status_code == 200, response.text
        # Original mobile production endpoint still requires order and station.
        assert client.get(f'/api/mobile/erp/products/{ids["product"]}/drawing').status_code == 403
    with factory() as db:
        user = db.scalar(select(User).where(User.username == "mobile-picker"))
        assert drawings.product_drawing_metadata(db, [ids["product"]], user=user,
                   visible_customer_ids=None)[ids["product"]]["status"] == "available"


def test_permission_scope_direct_guess_and_revocation_fail_closed(drawing_app):
    app, ids, factory, _ = drawing_app
    reader(factory, None)
    with TestClient(app) as client:
        unauthorized = client.get(urls(ids) + "/preview")
        assert unauthorized.status_code == 401 and "no-store" in unauthorized.headers["cache-control"]
        _login(client, "mobile-picker")
        denied = client.get(urls(ids) + "/preview")
        assert denied.status_code == 403 and "no-store" in denied.headers["cache-control"]
        _login(client, "mobile-scoped")
        for base in (f'/api/mobile/erp/products/{ids["other_product"]}/drawings/attachment-{ids["attachment"]}',
                     f'/api/mobile/erp/products/999999/drawings/attachment-{ids["attachment"]}'):
            response = client.get(base + "/preview")
            assert response.status_code == 404
            assert response.json()["detail"] == "产品不存在或当前账号无权查看"
        assert client.get(urls(ids) + "/preview").status_code == 200
        with factory() as db:
            db.get(Product, ids["product"]).is_active = False
            db.commit()
        assert client.get(urls(ids) + "/preview").status_code == 404


@pytest.mark.parametrize("reference", ["private:../outside.png", "/etc/secret.png", "https://example.org/a.png",
                                      "private:missing.png", "private:unsafe.svg", "private:drawing_originals/secret.png"])
def test_bad_paths_missing_files_and_artwork_have_readable_4xx(drawing_app, reference):
    app, ids, factory, root = drawing_app
    Image.new("RGB", (10, 10)).save(root.parent / "outside.png")
    with factory() as db:
        db.get(ProductDrawing, ids["attachment"]).image_path = reference
        db.commit()
    with TestClient(app) as client:
        _login(client, "mobile-admin")
        for mode in ("preview", "original"):
            response = client.get(urls(ids) + "/" + mode)
            assert response.status_code == 404, response.text
            assert "no-store" in response.headers["cache-control"]
            assert reference not in response.text


def test_attachment_identity_cannot_cross_products_or_read_artwork(drawing_app):
    app, ids, _, _ = drawing_app
    with TestClient(app) as client:
        _login(client, "mobile-admin")
        for key in (f'attachment-{ids["artwork"]}', f'attachment-{ids["pdf"]}', "legacy", "nonsense"):
            response = client.get(f'/api/mobile/erp/products/{ids["product"]}/drawings/{key}/preview')
            assert response.status_code == 404


@pytest.mark.parametrize("kind", ["damaged", "oversize-pixels"])
def test_images_cannot_render_html_svg_or_unbounded_pixels(drawing_app, kind):
    app, ids, factory, root = drawing_app
    if kind == "damaged":
        (root / "engineering.png").write_bytes(b"<svg xmlns='http://www.w3.org/2000/svg' onload='alert(1)'></svg>")
    else:
        Image.new("1", (5001, 5001)).save(root / "engineering.png")
    with factory() as db:
        db.get(ProductDrawing, ids["attachment"]).thumbnail_path = "private:engineering.png"
        db.commit()
    with TestClient(app) as client:
        _login(client, "mobile-admin")
        for mode in ("preview", "original"):
            response = client.get(urls(ids) + "/" + mode)
            assert response.status_code == 422 and "no-store" in response.headers["cache-control"]


def test_no_engineering_drawings_has_explicit_empty_state(drawing_app):
    app, ids, factory, _ = drawing_app
    with factory() as db:
        db.delete(db.get(ProductDrawing, ids["attachment"]))
        db.commit()  # Only the print artwork remains; it cannot become a drawing.
    with TestClient(app) as client:
        _login(client, "mobile-admin")
        response = client.get(f'/api/mobile/erp/products/{ids["product"]}/inventory')
        assert response.json()["product"]["drawings"] == {"status": "none", "items": []}


def test_corrupt_thumbnail_falls_back_to_valid_engineering_original(drawing_app):
    app, ids, _, root = drawing_app
    (root / "thumb.webp").write_bytes(b"broken thumbnail")
    with TestClient(app) as client:
        _login(client, "mobile-admin")
        response = client.get(urls(ids) + "/preview")
        assert response.status_code == 200, response.text
        with Image.open(BytesIO(response.content)) as image:
            assert image.size == (640, 480)


@pytest.mark.parametrize("pdf", [False, True])
def test_original_deleted_during_read_returns_readable_404(drawing_app, monkeypatch, pdf):
    from app.api import mobile_erp
    app, ids, _, root = drawing_app
    real_resolve = mobile_erp.drawing_file
    def disappearing(source):
        path = real_resolve(source)
        path.unlink()
        return path
    monkeypatch.setattr(mobile_erp, "drawing_file", disappearing)
    with TestClient(app) as client:
        _login(client, "mobile-admin")
        response = client.get(urls(ids, pdf=pdf) + "/original")
        assert response.status_code == 404, response.text
        assert "no-store" in response.headers["cache-control"] and str(root) not in response.text


@pytest.mark.parametrize("pdf", [False, True])
@pytest.mark.parametrize("change", ["delete", "replace"])
def test_original_after_media_validation_sends_same_verified_bytes(drawing_app, monkeypatch, pdf, change):
    app, ids, _, root = drawing_app
    path = root / ("engineering.pdf" if pdf else "engineering.png")
    expected = path.read_bytes()
    real_media_type = drawings.original_media_type
    def mutate_after_validation(content, suffix):
        result = real_media_type(content, suffix)
        if change == "delete":
            path.unlink()
        else:
            path.write_bytes(b"<svg onload='alert(1)'></svg>")
        return result
    monkeypatch.setattr(drawings, "original_media_type", mutate_after_validation)
    with TestClient(app) as client:
        _login(client, "mobile-admin")
        response = client.get(urls(ids, pdf=pdf) + "/original")
        assert response.status_code == 200, response.text
        assert response.content == expected
        assert response.headers["content-type"] == ("application/pdf" if pdf else "image/png")
        assert "no-store" in response.headers["cache-control"]


@pytest.mark.parametrize("pdf", [False, True])
def test_original_growth_after_stat_still_uses_bounded_read(drawing_app, monkeypatch, pdf):
    from app.api import mobile_erp
    app, ids, _, _ = drawing_app
    real_resolve = mobile_erp.drawing_file
    def grow_after_stat(source):
        path = real_resolve(source)
        with path.open("wb") as handle:
            handle.truncate(drawings.MAX_BYTES + 1)
        return path
    monkeypatch.setattr(mobile_erp, "drawing_file", grow_after_stat)
    with TestClient(app) as client:
        _login(client, "mobile-admin")
        response = client.get(urls(ids, pdf=pdf) + "/original")
        assert response.status_code == 413 and "no-store" in response.headers["cache-control"]


@pytest.mark.parametrize("kind", ["damaged", "encrypted", "oversize-page", "oversize-file"])
def test_pdf_bounds_and_failures(drawing_app, kind):
    app, ids, _, root = drawing_app
    path = root / "engineering.pdf"
    if kind == "damaged":
        path.write_bytes(b"%PDF-1.7\ninvalid")
    elif kind == "oversize-file":
        with path.open("wb") as handle:
            handle.truncate(drawings.MAX_BYTES + 1)
    else:
        doc = fitz.open()
        doc.new_page(width=20000 if kind == "oversize-page" else 300, height=300)
        doc.save(path, encryption=fitz.PDF_ENCRYPT_AES_256 if kind == "encrypted" else fitz.PDF_ENCRYPT_NONE,
                 owner_pw="owner", user_pw="secret")
        doc.close()
    with TestClient(app) as client:
        _login(client, "mobile-admin")
        for mode in ("preview", "original"):
            response = client.get(urls(ids, pdf=True) + "/" + mode)
            assert response.status_code == (413 if kind == "oversize-file" else 422), response.text
            assert "no-store" in response.headers["cache-control"]
            assert str(root) not in response.text


def test_related_orders_molds_inventory_and_dimension_stock_use_actual_product_ids(drawing_app):
    app, ids, _, _ = drawing_app
    with TestClient(app) as client:
        _login(client, "mobile-admin")
        for category in ("orders", "molds", "inventory"):
            response = client.get(f"/api/mobile/erp/search?q=MOBILE&category={category}")
            assert response.status_code == 200, response.text
            items = response.json()["groups"][0]["items"]
            candidates = [p for item in items for p in item.get("products", [item])]
            assert any(p.get("product_id") == ids["product"] and p["drawings"]["status"] == "available" for p in candidates)
        detail = client.get(f'/api/mobile/erp/products/{ids["product"]}/inventory')
        assert detail.json()["product"]["drawings"]["status"] == "available"
        response = client.get("/api/mobile/erp/warehouse/dimension-stock?kind=box&length=420&length_op=eq")
        assert response.status_code == 200, response.text
        item = next(row for row in response.json()["items"] if row["id"] == ids["finished_lot"])
        assert item["product_id"] == ids["product"] and item["drawings"]["status"] == "available"
        response = client.get(f'/api/mobile/erp/warehouse/dimension-stock/{ids["finished_lot"]}/detail')
        assert response.status_code == 200, response.text
        assert response.json()["item"]["drawings"]["status"] == "available"


def test_published_sources_match_current_product_customer_version_and_metadata_queries_are_constant(drawing_app):
    app, ids, factory, _ = drawing_app
    with factory() as db:
        product = db.get(Product, ids["product_two"])
        db.delete(db.get(ProductDrawing, ids["pdf"]))
        db.add(DrawingRelease(product_id=product.id, customer_id=product.customer_id, revision="R1",
            external_number="SYNTHETIC", internal_number="TEST-1", idempotency_key="synthetic-key",
            design_version=1, product_version=product.version, template_key="liner_v1", manifest_json="{}",
            pdf_reference="private:engineering.pdf", pdf_sha256="0" * 64))
        db.commit()
        user = db.scalar(select(User).where(User.username == "mobile-admin"))
        calls = []
        def capture(*args):
            calls.append(args[2])
        event.listen(db.bind, "before_cursor_execute", capture)
        try:
            one = drawings.product_drawing_metadata(db, [product.id], user=user, visible_customer_ids=None)
            single_count = len(calls)
            calls.clear()
            many = drawings.product_drawing_metadata(db, list(range(1, 101)), user=user, visible_customer_ids=None)
            assert len(calls) == single_count == 3
        finally:
            event.remove(db.bind, "before_cursor_execute", capture)
        release_key = one[product.id]["items"][0]["id"]
        assert release_key.startswith("release-") and many[product.id] == one[product.id]
        product.version += 1
        db.commit()
    with TestClient(app) as client:
        _login(client, "mobile-admin")
        response = client.get(f'/api/mobile/erp/products/{ids["product_two"]}/drawings/{release_key}/preview')
        assert response.status_code == 404  # Frozen tasks retain their separate release URL.


def test_all_uploaded_engineering_drawings_are_listed_and_individually_readable(drawing_app, monkeypatch):
    app, ids, factory, root = drawing_app
    with factory() as db:
        original = db.get(ProductDrawing, ids["attachment"])
        original.uploaded_at = datetime(2026, 10, 1, 9)
        # Two uploads at the same time still have a stable order; a missing
        # newest file must not hide the earlier, valid engineering drawings.
        pdf = ProductDrawing(product_id=ids["product"], image_path="private:engineering.pdf",
                             thumbnail_path="private:engineering.pdf", uploaded_at=datetime(2026, 10, 2, 9))
        missing = ProductDrawing(product_id=ids["product"], image_path="private:missing.png",
                                 thumbnail_path="private:missing.png", uploaded_at=datetime(2026, 10, 2, 9))
        db.add_all([pdf, missing])
        db.commit()
        expected = [f"attachment-{missing.id}", f"attachment-{pdf.id}", f"attachment-{original.id}"]
    with TestClient(app) as client:
        _login(client, "mobile-admin")
        with monkeypatch.context() as patch:
            patch.setattr(drawings, "resolve_stored_reference", lambda *_: pytest.fail("Metadata opened a file"))
            product = client.get(f'/api/mobile/erp/products/{ids["product"]}/inventory').json()["product"]
            items = product["drawings"]["items"]
            assert [item["id"] for item in items] == expected
            assert [item["uploaded_at"] for item in items] == ["2026-10-02T09:00:00Z", "2026-10-02T09:00:00Z", "2026-10-01T09:00:00Z"]
            assert "private:" not in json.dumps(product)
        assert client.get(items[0]["preview_url"]).status_code == 404
        for item in items[1:]:
            assert client.get(item["preview_url"]).status_code == 200
            assert client.get(item["original_url"]).status_code == 200
        # All upload records survive a missing file and preview failures.
        assert len(client.get(f'/api/mobile/erp/products/{ids["product"]}/inventory').json()["product"]["drawings"]["items"]) == 3
        overview = client.get(f'/api/mobile/erp/products/{ids["product"]}/production-overview')
        assert overview.status_code == 200, overview.text
        assert [item["id"] for item in overview.json()["product"]["drawings"]["items"]] == expected
    with factory() as db:
        user = db.scalar(select(User).where(User.username == "mobile-admin"))
        denied = drawings.product_drawing_metadata(db, [ids["product"]], user=user, visible_customer_ids=[])
        assert denied[ids["product"]] == {"status": "none", "items": []}
        assert db.get(Product, ids["product"]).version == 1


def test_uploaded_history_does_not_duplicate_legacy_pointer_or_include_print_artwork(drawing_app):
    app, ids, factory, _ = drawing_app
    with factory() as db:
        db.get(Product, ids["product"]).die_cut_path = "private:engineering.png"
        db.commit()
    with TestClient(app) as client:
        _login(client, "mobile-admin")
        product = client.get(f'/api/mobile/erp/products/{ids["product"]}/inventory').json()["product"]
        assert [item["id"] for item in product["drawings"]["items"]] == [f'attachment-{ids["attachment"]}']
